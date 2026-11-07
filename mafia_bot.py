"""
Misumi AI — Mafia o'yini, Telegram tomoni
-----------------------------------------
/mafia            — guruhda lobbi ochadi (o'yinchilar «Qo'shilish» tugmasi orqali botga o'tib,
                    Mini App'ni ochadi)
/mafiastop        — o'yinni bekor qilish (ega yoki guruh admini)
/mafiarules       — rollar va qoidalar

Misumi ikki rolda:
  • BOSHLOVCHI — tun/tong/osish/yakunni guruhga o'zi e'lon qiladi (AI bilan, tez ishlamasa — tayyor matn),
  • O'YINCHI   — lobbiga «Misumi'ni qo'shish» bilan kiradi: tunda harakat qiladi, kunduzi chatda gapiradi,
                 ovoz beradi. Uning roli hech kimga ma'lum emas.

O'yin mexanikasi mafia.py da, Mini App API mafia_api.py da.
"""
import asyncio
import html
import logging
import os
import random
import re
from collections import Counter, defaultdict

from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup, Update, WebAppInfo
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

import ai_core
import mafia

logger = logging.getLogger("misumi-mafia")

WEBAPP_URL = (os.environ.get("WEBAPP_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
ADMIN_ID = os.environ.get("ADMIN_ID", "").strip()
_GROUPS = filters.ChatType.GROUPS
_PRIVATE = filters.ChatType.PRIVATE


def _e(s) -> str:
    return html.escape(str(s))


def _deeplink(bot_username: str, gid: str) -> str:
    return f"https://t.me/{bot_username}?start=mafia_{gid}"


def _app_url(gid: str) -> str | None:
    return f"{WEBAPP_URL}/mafia?g={gid}" if WEBAPP_URL else None


def _role_label(role: str) -> str:
    r = mafia.ROLES[role]
    return f"{r['emoji']} {r['name']}"


def _open_kb(bot_username: str, gid: str, text: str = "🎭 O'yinni ochish") -> Markup:
    return Markup([[Btn(text, url=_deeplink(bot_username, gid))]])


def _dm_kb(gid: str, text: str = "🎭 O'yinni ochish") -> Markup | None:
    url = _app_url(gid)
    return Markup([[Btn(text, web_app=WebAppInfo(url=url))]]) if url else None


# ── lobbi matni va tugmalari ──────────────────────────────────────────
def _lobby_text(g: "mafia.Game") -> str:
    lines = []
    for i, p in enumerate(g.players.values(), 1):
        tag = " 👑" if p.uid == g.host_id else ""
        icon = "🤖 " if p.is_bot else ""
        lines.append(f"{i}. {icon}{_e(p.name)}{tag}")
    n = len(g.players)
    need = ""
    if n < mafia.MIN_PLAYERS:
        need = f"\n\nYana kamida <b>{mafia.MIN_PLAYERS - n}</b> ta o'yinchi kerak."
    return (
        "🎭 <b>MAFIA</b> — o'yin lobbisi\n\n"
        f"O'yinchilar (<b>{n}/{mafia.MAX_PLAYERS}</b>):\n" + "\n".join(lines) + need +
        "\n\n«Qo'shilish» ni bosing → bot ochiladi → Mini App tugmasi chiqadi."
    )


def _lobby_kb(g: "mafia.Game", bot_username: str) -> Markup:
    rows = [[Btn("🎮 Qo'shilish", url=_deeplink(bot_username, g.gid))]]
    row2 = []
    if mafia.BOT_UID not in g.players:
        row2.append(Btn("🤖 Misumi'ni qo'shish", callback_data=f"mf:bot:{g.gid}"))
    row2.append(Btn("▶️ Boshlash", callback_data=f"mf:go:{g.gid}"))
    rows.append(row2)
    rows.append([Btn("✖️ Bekor qilish", callback_data=f"mf:x:{g.gid}")])
    return Markup(rows)


async def _can_manage(context, g: "mafia.Game", user_id: int) -> bool:
    if user_id == g.host_id or (ADMIN_ID and str(user_id) == ADMIN_ID):
        return True
    try:
        m = await context.bot.get_chat_member(g.chat_id, user_id)
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except TelegramError:
        return False


# ── buyruqlar ─────────────────────────────────────────────────────────
async def cmd_mafia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    g = mafia.active_game(chat.id)
    if g and g.phase != "lobby":
        await msg.reply_text(
            f"Bu guruhda o'yin ketmoqda ({g.day}-kun/tun). Tugashini kut yoki ega /mafiastop qilsin.",
            reply_markup=_open_kb(context.bot.username, g.gid),
        )
        return
    if not g:
        g = mafia.create_game(chat.id, user.id, user.first_name or "Ega", chat.title or "", user.username)
        g.outbox.clear()  # birinchi "lobby" eventi kerak emas — pastda yangi xabar yuboramiz
    sent = await msg.reply_text(
        _lobby_text(g), parse_mode=ParseMode.HTML,
        reply_markup=_lobby_kb(g, context.bot.username),
    )
    g.lobby_msg_id = sent.message_id


async def cmd_mafiastop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    g = mafia.active_game(update.effective_chat.id)
    if not g:
        await update.effective_message.reply_text("Hozir faol mafia o'yini yo'q.")
        return
    if not await _can_manage(context, g, update.effective_user.id):
        await update.effective_message.reply_text("Buni faqat o'yin egasi yoki guruh admini qila oladi.")
        return
    g.cancel("O'yin egasi o'yinni to'xtatdi")


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["🎭 <b>MAFIA — qoidalar</b>\n",
             "Shahar mafiyani topishi, mafiya esa shaharni qirib tashlashi kerak. "
             f"{mafia.MIN_PLAYERS}–{mafia.MAX_PLAYERS} o'yinchi.\n",
             "🌙 <b>Tun</b>: rollar yashirin harakat qiladi.\n"
             "☀️ <b>Kun</b>: muhokama, keyin ovoz berish. Eng ko'p ovoz olgan osiladi.\n"]
    for r in mafia.ROLES.values():
        lines.append(f"{r['emoji']} <b>{r['name']}</b> — {_e(r['desc'])}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cb_lobby(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, action, gid = q.data.split(":", 2)
    g = mafia.get_game(gid)
    if not g or g.phase == "ended":
        await q.answer("Bu o'yin tugagan.", show_alert=True)
        return
    uid = q.from_user.id
    if not await _can_manage(context, g, uid):
        await q.answer("Bu tugma faqat o'yin egasi uchun.", show_alert=True)
        return
    if action == "bot":
        ok, m = g.add_bot()
        await q.answer("Misumi o'yinga qo'shildi 🤖" if ok else m, show_alert=not ok)
    elif action == "go":
        ok, m = g.start(g.host_id, force=True)
        await q.answer("Boshlandi!" if ok else m, show_alert=not ok)
    elif action == "x":
        g.cancel("Lobbi bekor qilindi")
        await q.answer("Bekor qilindi")


async def dm_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start mafia_<gid> — guruhdagi «Qo'shilish» tugmasidan keladi."""
    args = context.args or []
    if not args or not args[0].startswith("mafia_"):
        return  # oddiy /start — boshqa handler ishlaydi
    msg, user = update.effective_message, update.effective_user
    g = mafia.get_game(args[0][len("mafia_"):])
    if not g or g.phase == "ended":
        await msg.reply_text("Bu o'yin topilmadi yoki allaqachon tugagan. Guruhda /mafia deb yangisini och.")
        raise ApplicationHandlerStop
    if user.id not in g.players:
        ok, m = g.add_player(user.id, user.first_name or "O'yinchi", user.username)
        if not ok:
            await msg.reply_text(m)
            raise ApplicationHandlerStop
    kb = _dm_kb(g.gid)
    if g.phase == "lobby":
        text = (f"✅ Sen <b>Mafia</b> lobbisidasan ({len(g.players)}/{mafia.MAX_PLAYERS}). "
                "Boshlanishini kut, Mini App'ni hozirdan ochib qo'yish mumkin.")
    else:
        text = "🎭 O'yin davom etyapti. Mini App'ni och:"
    if kb is None:
        text += "\n\n⚠️ WEBAPP_URL sozlanmagan, Mini App tugmasi chiqmaydi."
    await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    raise ApplicationHandlerStop


# ── AI bilan e'lon (tez ishlamasa — tayyor matn) ──────────────────────
async def _narrate(facts: str, fallback: str, timeout: float = 8.0) -> str:
    instruction = (
        "Sen Mafia o'yinining boshlovchisisan (Misumi). Voqeani guruhga 1-2 QISQA gapda e'lon qil: "
        "qorong'u, kinoyali hikoyachi ohangi, ko'cha uslubi, sen-lab. Faqat berilgan faktlardagi ismlarni "
        "ishlat, hech narsa o'ylab topma. Kimning qanday rolda ekanini o'zing aytma. Emoji yozma. "
        "Faqat e'lon matnini yoz."
    )
    try:
        txt = await asyncio.wait_for(asyncio.to_thread(ai_core._general_call, instruction, facts, ""), timeout)
    except Exception:
        return fallback
    txt = " ".join((txt or "").split())
    if not txt or len(txt) > 320:
        return fallback
    return txt


_NIGHT_LINES = [
    "Shahar uxladi. Ko'chalar jim, faqat bir-ikki ko'z ochiq.",
    "Qorong'u tushdi. Kimdir uxlayapti, kimdir esa yo'q.",
    "Chiroqlar o'chdi. Bu tunni hamma ham ko'rmaydi.",
]
_QUIET_LINES = [
    "Tong otdi, hamma tirik. Yoki hozircha shunday ko'rinadi.",
    "Bu tun jim o'tdi. Juda jim. Shubhali.",
]
_DEAD_LINES = [
    "Tong otdi, lekin hamma ham uyg'onmadi.",
    "Quyosh chiqdi, bir kishi esa qaytib uyg'onmadi.",
]


async def _say(bot, chat_id: int, text: str, kb=None) -> None:
    try:
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb,
                               disable_web_page_preview=True)
    except TelegramError as e:
        logger.warning(f"[mafia] guruhga yuborib bo'lmadi {chat_id}: {e}")


async def _dm(bot, uid: int, text: str, kb=None) -> None:
    if uid == mafia.BOT_UID:
        return
    try:
        await bot.send_message(uid, text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except TelegramError as e:
        logger.info(f"[mafia] DM yuborilmadi {uid}: {e}")


# ── Misumi o'yinchi sifatida gapiradi ─────────────────────────────────
_LEAK_RE = re.compile(r"(mafiya|komissar|doktor|don|maniak|qo'riqchi|kelin|fuqaro)\s*(man\b|ekanman|bo'lib)", re.I)


def _bot_context(g: "mafia.Game"):
    with mafia.LOCK:
        me = g.players.get(mafia.BOT_UID)
        if not me or not me.alive or g.phase not in ("day", "vote"):
            return None
        others = [p.name for p in g.alive_players() if p.uid != mafia.BOT_UID]
        recent = [f"{m['name']}: {m['text']}" for m in g.chat if m["ch"] == "day"][-6:]
        deaths = [f["text"] for f in g.feed if f["kind"] in ("death", "lynch")][-2:]
        return {"role": me.role, "team": me.team, "others": others, "recent": recent,
                "deaths": deaths, "day": g.day, "checked": dict(me.checked),
                "names": {u: p.name for u, p in g.players.items()}}


async def _bot_say(g: "mafia.Game") -> None:
    c = await asyncio.to_thread(_bot_context, g)
    if not c or not c["others"]:
        return
    role_name = mafia.ROLES[c["role"]]["name"]
    info = ""
    if c["role"] == "commissar" and c["checked"]:
        mafs = [c["names"][u] for u, r in c["checked"].items() if r == "mafia"]
        if mafs:
            info = f" Tekshiruvingda {', '.join(mafs)} mafiya chiqdi."
    instruction = (
        "Sen Mafia o'yinida oddiy o'yinchisan (ismingiz Misumi). Rolingni HECH QACHON ochiq aytma; "
        "mafiya bo'lsang begunoh va ishonchli ko'rin, o'zgalarga shubha uyg'ot. "
        "BITTA juda qisqa xabar yoz (12 so'zdan oshmasin): kimnidir gumon qil, o'zingni himoya qil "
        "yoki oldingi gapga javob ber. Ko'cha uslubi, sen-lab, emojisiz. Faqat xabar matnini yoz."
    )
    prompt = (
        f"Sening rolling (yashirin): {role_name}.{info}\n"
        f"{c['day']}-kun. Tirik o'yinchilar: {', '.join(c['others'])}.\n"
        f"So'nggi voqealar: {' | '.join(c['deaths']) or 'yo`q'}\n"
        f"Oxirgi gaplar:\n" + ("\n".join(c["recent"]) or "(hali hech kim gapirmadi)")
    )
    fallbacks = [
        f"{random.choice(c['others'])} nimadir yashiryapti, ko'zimni olmayman",
        "shoshmay ovoz beraylik, keyin pushaymon bo'lamiz",
        f"{random.choice(c['others'])}, sen nega jimsan?",
        "bu tunni kim qilganini bilaman deb o'ylayman",
        "hamma bir-biriga shubha qilyapti, mafiya kulib o'tiribdi",
    ]
    try:
        text = await asyncio.wait_for(asyncio.to_thread(ai_core._general_call, instruction, prompt, ""), 9)
    except Exception:
        text = ""
    text = " ".join((text or "").split()).strip('"«»')
    if not text or len(text) > 160 or _LEAK_RE.search(text):
        text = random.choice(fallbacks)
    g.post_chat(mafia.BOT_UID, text)


# ── eventlarni qayta ishlash ──────────────────────────────────────────
async def _handle(context: ContextTypes.DEFAULT_TYPE, g: "mafia.Game", ev: dict) -> None:
    bot = context.bot
    bu = bot.username
    k = ev["kind"]
    chat = g.chat_id
    gk = _open_kb(bu, g.gid)

    if k == "lobby":
        if g.lobby_msg_id and g.phase == "lobby":
            try:
                await bot.edit_message_text(
                    _lobby_text(g), chat_id=chat, message_id=g.lobby_msg_id,
                    parse_mode=ParseMode.HTML, reply_markup=_lobby_kb(g, bu),
                )
            except BadRequest as e:
                if "not modified" not in str(e).lower():
                    logger.warning(f"[mafia] lobbi yangilanmadi: {e}")
        return

    if k == "start":
        if g.lobby_msg_id:
            try:
                await bot.edit_message_reply_markup(chat, g.lobby_msg_id, reply_markup=None)
            except TelegramError:
                pass
        with mafia.LOCK:
            comp = Counter(p.role for p in g.players.values())
            has_bot = mafia.BOT_UID in g.players
            n = len(g.players)
            roster = [(p.uid, p.role, p.team, p.is_bot) for p in g.players.values()]
            mafia_names = [p.name for p in g.players.values() if p.team == "mafia"]
        comp_text = ", ".join(f"{mafia.ROLES[r]['emoji']} {mafia.ROLES[r]['name']} ×{c}" if c > 1
                              else f"{mafia.ROLES[r]['emoji']} {mafia.ROLES[r]['name']}" for r, c in comp.items())
        extra = "\n🤖 Misumi ham o'ynayapti — u kim ekanini hech kim bilmaydi." if has_bot else ""
        await _say(bot, chat,
                   f"🎭 <b>MAFIA boshlandi!</b> {n} ta o'yinchi.\n"
                   f"Rollar: {comp_text}{extra}\n\nRolingni Mini App'dan ko'r. {mafia.REVEAL_SEC} soniyadan keyin tun tushadi.",
                   gk)
        for uid, role, team, is_bot in roster:
            if is_bot:
                continue
            r = mafia.ROLES[role]
            text = f"{r['emoji']} <b>Sening rolling: {r['name']}</b>\n\n{_e(r['desc'])}"
            if team == "mafia" and len(mafia_names) > 1:
                text += f"\n\n🔫 Mafiya jamoasi: <b>{_e(', '.join(mafia_names))}</b>"
            await _dm(bot, uid, text, _dm_kb(g.gid))
        return

    if k == "night":
        await _say(bot, chat, f"🌙 <b>{ev['day']}-tun.</b> {random.choice(_NIGHT_LINES)}\n"
                              "Rollar harakat qilmoqda (Mini App).", gk)
        with mafia.LOCK:
            actors = [p.uid for p in g.alive_players() if p.action and not p.is_bot]
        for uid in actors:
            await _dm(bot, uid, "🌙 Tun boshlandi. Harakatingni tanla:", _dm_kb(g.gid, "🌙 Harakat qilish"))
        return

    if k == "morning":
        deaths = ev["deaths"]
        if deaths:
            names = ", ".join(d["name"] for d in deaths)
            facts = f"{ev['day']}-tun tugadi, tong otdi. Tunda o'ldirilgan: {names}."
            fb = random.choice(_DEAD_LINES)
            facts_block = "\n".join(f"☠️ <b>{_e(d['name'])}</b> — {_role_label(d['role'])}" for d in deaths)
        else:
            facts = f"{ev['day']}-tun tugadi, tong otdi. Hech kim o'lmadi."
            fb = random.choice(_QUIET_LINES)
            facts_block = "Bu tun hech kim o'lmadi."
        flavor = await _narrate(facts, fb)
        with mafia.LOCK:
            ended = g.phase == "ended"
            alive_n = len(g.alive_players())
        tail = "" if ended else f"\n\n☀️ <b>{ev['day']}-kun.</b> Tirik: {alive_n}. Muhokama {mafia.DAY_SEC} soniya (Mini App)."
        await _say(bot, chat, f"{_e(flavor)}\n\n{facts_block}{tail}", None if ended else gk)
        return

    if k == "vote_start":
        await _say(bot, chat, f"🗳 <b>Ovoz berish vaqti!</b> {mafia.VOTE_SEC} soniyang bor. "
                              "Kimni osamiz? Mini App'da ovoz ber.", gk)
        return

    if k == "lynch":
        flavor = await _narrate(f"Shahar ovoz berib {ev['name']} ni osdi.",
                                "Shahar qaror qildi. Arqon tortildi.")
        await _say(bot, chat, f"{_e(flavor)}\n\n⚖️ <b>{_e(ev['name'])}</b> osildi — {_role_label(ev['role'])}.\n"
                              f"<i>Ovozlar: {_e(ev['summary'])}</i>")
        return

    if k == "no_lynch":
        await _say(bot, chat, f"⚖️ Ovozlar bo'linib ketdi, hech kim osilmadi.\n<i>Ovozlar: {_e(ev['summary'])}</i>")
        return

    if k == "end":
        w = ev["winner"]
        titles = {"town": "🏙 <b>SHAHAR g'alaba qildi!</b>", "mafia": "🔫 <b>MAFIYA g'alaba qildi!</b>",
                  "maniac": "🔪 <b>MANIAK g'alaba qildi!</b>"}
        facts = {"town": "Barcha jinoyatchilar topildi, shahar tinchidi.",
                 "mafia": "Mafiya shaharni egalladi.", "maniac": "Oxirgi tirik qolgan Maniak bo'ldi."}[w]
        flavor = await _narrate(facts, facts)
        with mafia.LOCK:
            rows = []
            for p in g.players.values():
                mark = "✅" if p.alive else "💀"
                bot_mark = "🤖 " if p.is_bot else ""
                rows.append(f"{mark} {bot_mark}{_e(p.name)} — {_role_label(p.role)}")
        await _say(bot, chat, f"{titles[w]}\n{_e(flavor)}\n\n<b>Rollar:</b>\n" + "\n".join(rows) +
                              "\n\nYana o'ynash uchun /mafia")
        return

    if k == "cancel":
        await _say(bot, chat, f"🛑 {_e(ev.get('reason', 'O`yin bekor qilindi'))}")
        return

    if k == "bot_say":
        await _bot_say(g)
        return


async def _process_game_events(context, g, events) -> None:
    for ev in events:
        try:
            await _handle(context, g, ev)
        except Exception:
            logger.exception("[mafia] event xatosi: %s", ev.get("kind"))


async def mafia_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        mafia.tick_all()
        pending = mafia.drain_outbox()
    except Exception:
        logger.exception("[mafia] tick xatosi")
        return
    by_game: dict[str, list] = defaultdict(list)
    games: dict[str, "mafia.Game"] = {}
    seen_lobby: set[str] = set()
    for g, ev in pending:
        if ev["kind"] == "lobby":
            if g.gid in seen_lobby:
                continue
            seen_lobby.add(g.gid)
        by_game[g.gid].append(ev)
        games[g.gid] = g
    for gid, evs in by_game.items():
        # har o'yin o'z navbatida (tartib saqlanadi), o'yinlar bir-birini kutmaydi
        context.application.create_task(_process_game_events(context, games[gid], evs))


def register(app) -> None:
    app.add_handler(CommandHandler("start", dm_start, filters=_PRIVATE), group=-3)
    app.add_handler(CommandHandler(["mafia", "mafiya"], cmd_mafia, filters=_GROUPS), group=0)
    app.add_handler(CommandHandler("mafiastop", cmd_mafiastop, filters=_GROUPS), group=0)
    app.add_handler(CommandHandler("mafiarules", cmd_rules), group=0)
    app.add_handler(CallbackQueryHandler(cb_lobby, pattern=r"^mf:"), group=0)
    app.job_queue.run_repeating(mafia_job, interval=1.0, first=3)
