"""
Misumi AI — fun.py
Barcha qo'shimcha guruh komandalar:

🎮 O'yinlar:
  /truth        — guruhdan kimgadir "rostini ayt" savol beradi
  /dare         — "bajara olasanmi" topshiriq
  /wouldyourather — ikki tanlov, guruh ovoz beradi
  /8ball        — sehr shari, savolga javob beradi
  /rps          — tosh-qaychi-qog'oz o'yini

🎲 Tasodif / Fun:
  /roast @user  — AI yozgan kulgili "haqorat" (do'stona)
  /compliment @user — AI yozgan chiroyli maqtov
  /horoscope    — bugungi yulduz burji bashorati
  /lucky        — bugungi omad raqami va rangi
  /rate @user   — tasodifiy "baholash" (aqlli, kuchli, chiroyli %)

💬 Guruh hayoti:
  /quote        — AI yozgan motivatsion gap
  /confession   — anonim e'tirof (bot nomidan yozadi)
  /ngl          — "rostini aytganda" — tasodifiy a'zo haqida gap
  /fakechat     — ikki a'zo o'rtasida AI o'ylab topgan suhbat

📊 Statistika:
  /stats        — guruhning faollik statistikasi
  /who          — "kim eng ko'p xabar yozadi?"
  /activity     — oxirgi 7 kunda guruh faolligi

🌍 Foydali:
  /translate    — xabarni tarjima qilish
  /weather      — ob-havo (shahar nomi bilan)
  /calc         — kalkulyator
  /remind       — eslatma o'rnatish

💔 Ship qo'shimchalari:
  /divorced     — ajrashish (kulgili format, mol-mulk taqsimoti)
  /crush @user  — yashirin sevgi e'lon qilish
  /soulmate     — ruhiy sherik (telepatiya, o'tgan hayot)
  /exship       — eski juft — ajrashish tarixi
  /couple       — /erxotin ning inglizcha versiyasi
  /husband @user — kimning eri?
  /wife @user   — kimning xotini?
"""

import asyncio
import math
import random
import time
from collections import defaultdict
from datetime import datetime, timedelta

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import ai_core

# ── ship modulidan kerakli narsalar ──────────────────────────────────────────
try:
    from ship import (
        _seen_members, _mention, _get_couple_image, _pick_two,
        _couple_bar, _ship_name, _gen_couple_caption,
        load_member_facts, _last_ship_group, GROUP_COOLDOWN, _fmt_time,
        ZODIAC_SIGNS, _married_couples, _members_by_gender, get_gender
    )
    _ship_available = True
except Exception as e:
    print(f"[fun] ship import error: {e}")
    _ship_available = False
    _seen_members = defaultdict(dict)
    _last_ship_group = {}
    GROUP_COOLDOWN = 60
    _married_couples = defaultdict(list)

    def _mention(uid, name, username):
        return f"@{username}" if username else name

    def _fmt_time(s):
        s = int(s)
        m, sec = divmod(s, 60)
        return f"{m} daqiqa {sec} soniya" if m else f"{s} soniya"

    ZODIAC_SIGNS = [
        "♈ Qo'y", "♉ Ho'kiz", "♊ Egizaklar", "♋ Qisqichbaqa",
        "♌ Sher", "♍ Boshoq", "♎ Tarozi", "♏ Chayon",
        "♐ Sagitarius", "♑ Tog' echkisi", "♒ Qovg'a", "♓ Baliq"
    ]


# ── Global statistika ─────────────────────────────────────────────────────────
_msg_count: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
_daily_count: dict[int, dict[str, dict[int, int]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
_remind_tasks: list[dict] = []

# WouldYouRather ovozlari: {chat_id: {msg_id: {"A": set(), "B": set(), "q": str}}}
_wyr_votes: dict[int, dict[int, dict]] = defaultdict(dict)

# RPS o'yinlari: {chat_id: {msg_id: {"user_id": int, "choice": str}}}
_rps_games: dict[int, dict[int, dict]] = defaultdict(dict)

# ── Yordamchi ─────────────────────────────────────────────────────────────────

def _ai(instruction: str, prompt: str, fallback: str = "") -> str:
    """AI ga savol beradi, xato bo'lsa fallback qaytaradi."""
    try:
        for call in (ai_core._call_cerebras, ai_core._call_gemini, ai_core._call_groq):
            try:
                text = call(
                    ai_core.GENERAL_CHAT_PERSONA + "\n\n" + instruction,
                    [],
                    prompt,
                ).strip().strip('"').strip("'")
                if text and len(text) > 5:
                    return text
            except Exception as e:
                print(f"[fun:ai:{call.__name__}] {e}")
    except Exception:
        pass
    return fallback or "Hm, tushunmadim... 🤔"


def _track_msg(chat_id: int, user_id: int):
    today = datetime.now().strftime("%Y-%m-%d")
    _msg_count[chat_id][user_id] += 1
    _daily_count[chat_id][today][user_id] += 1


async def _group_only(message) -> bool:
    if message.chat.type not in ("group", "supergroup"):
        await message.reply_text("❌ Bu komanda faqat guruhlarda ishlaydi! 👥")
        return True
    return False


def _get_mention_user(message):
    """Xabardagi mention yoki reply dan user ma'lumotini oladi."""
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        return u.id, u.first_name or str(u.id), u.username
    if message.entities:
        for e in message.entities:
            if e.type == "text_mention" and e.user:
                u = e.user
                return u.id, u.first_name or str(u.id), u.username
    return None, None, None


# ═══════════════════════════════════════════════════════════════════════════════
# 🎮 O'YINLAR
# ═══════════════════════════════════════════════════════════════════════════════

# ── /truth ────────────────────────────────────────────────────────────────────
TRUTH_QUESTIONS = [
    "Hayotingdagi eng xijolatomuz lahza nima bo'lgan?",
    "Hech kimga aytmagan eng katta sirlaringdan birini ayt!",
    "O'zingni qaysi kamchiligingdan eng ko'p uyalasan?",
    "Birinchi muhabbating kim edi?",
    "Hayotingdagi eng katta yolg'oning nima?",
    "Qaysi do'stingni orzularingda ko'rgansen?",
    "Eng so'nggi yig'laganing qachon va nima uchun?",
    "Hayotingda eng ko'p afsuslanadigan qaroringni ayt.",
    "Hech kim bilmasin degan eng yashirin odatingni ayt.",
    "Bir kuni kim bilan almashib yashashni xohlagan bo'larding?",
    "Eng uzoq davom etgan yolg'oning nima?",
    "Birinchi o'pichingni eslaysan? Kim bilan edi?",
    "Qaysi mashhur odamni yoqtirasan? Uyalmasdan ayt!",
    "Hech kimga aytmagan eng katta orzuing nima?",
    "Bu guruhda eng ko'p kim bilan bahslashasan? Nega?",
]

async def truth_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if await _group_only(msg): return
    members = dict(_seen_members.get(msg.chat_id, {}))
    target = None
    if members:
        uid, mdata = random.choice(list(members.items()))
        target = _mention(uid, mdata["name"], mdata.get("username"))
    else:
        target = _mention(update.effective_user.id, update.effective_user.first_name, update.effective_user.username)

    question = random.choice(TRUTH_QUESTIONS)
    await msg.reply_text(
        f"🎯 <b>TRUTH</b> 🎯\n\n"
        f"👤 {target}!\n\n"
        f"❓ <b>{question}</b>\n\n"
        f"<i>Javob berish majburiy... agar jasur bo'lsang! 😏</i>",
        parse_mode="HTML"
    )


# ── /dare ─────────────────────────────────────────────────────────────────────
DARE_TASKS = [
    "Ushbu guruhda o'zingni sevimli qo'shig'ingni kuyla (voice yuborish shart!).",
    "Keyingi 5 xabaringni KATTA HARFLAR bilan yoz.",
    "Guruhda o'zingning 3 ta kuchli tomoningni maqta.",
    "Misumi botga munosib shoir bo'lib, 4 misrali she'r yoz.",
    "O'ng qo'lingngsiz 1 daqiqa xabar yoz.",
    "Guruh a'zolaridan biriga samimiy tabrik yoz.",
    "O'zing haqingda 5 ta qiziq fakt ayt.",
    "Eng sevimli filmingni bir jumla bilan tushuntir.",
    "Guruhning eng kuchli a'zosini tanlang va uni maqtang.",
    "Keyingi xabaringni teskari yozib ko'ring (oxirdan boshiga).",
    "O'zingning eng uyatlı holatini emoji bilan tasvirla.",
    "Guruh adminiga minnatdorchilik xabari yoz.",
    "Bu guruhda kimni eng ko'p sog'inarding? Unga ayt!",
    "Telefon kontaktlaringdan birinchi ismni ayt.",
    "Kelajak rejaingni 3 ta kalit so'z bilan ayt.",
]

async def dare_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if await _group_only(msg): return
    members = dict(_seen_members.get(msg.chat_id, {}))
    if members:
        uid, mdata = random.choice(list(members.items()))
        target = _mention(uid, mdata["name"], mdata.get("username"))
    else:
        target = _mention(update.effective_user.id, update.effective_user.first_name, update.effective_user.username)

    task = random.choice(DARE_TASKS)
    await msg.reply_text(
        f"💥 <b>DARE</b> 💥\n\n"
        f"👤 {target}!\n\n"
        f"🎯 <b>Vazifang:</b> {task}\n\n"
        f"<i>5 daqiqa ichida bajarmasa — uyat! 😄</i>",
        parse_mode="HTML"
    )


# ── /wouldyourather ───────────────────────────────────────────────────────────
WYR_PAIRS = [
    ("Umr bo'yi internet bo'lmasa yashash 🚫📱", "Umr bo'yi muzika tinglolmaslik 🚫🎵"),
    ("Doim yolg'on gapirish 🤥", "Hech qachon yolg'on gapirmaslik 😇"),
    ("O'tmishni o'zgartirish 🕐", "Kelajakni ko'rish 🔮"),
    ("Qaerga xohlasang uchish 🦅", "Suvda nafas olib yashash 🐬"),
    ("Hamma tillarni bilish 🌍", "Har qanday asbobni chalish 🎸"),
    ("Doim sovuqda yashash ❄️", "Doim issiqda yashash 🔥"),
    ("Kichik uyda boy bo'lish 🏠💰", "Katta saroydacha kambag'al bo'lish 🏰"),
    ("Cho'l qabilasida 1 yil 🏜️", "Okean tubida 1 yil 🌊"),
    ("Hamma sirlaringni biladigan bo'lsin 😱", "Hech kim seninqiyofangni eslamasin 👻"),
    ("Musiqasiz 1 yil 🎵❌", "Internetsiz 1 yil 📱❌"),
]

async def wouldyourather_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if await _group_only(msg): return
    a, b = random.choice(WYR_PAIRS)
    sent = await msg.reply_text(
        f"🤔 <b>WOULD YOU RATHER?</b>\n\n"
        f"🅰️ {a}\n\n<b>YO'QSA</b>\n\n🅱️ {b}\n\n"
        f"Ovoz bering! 👇",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🅰️ Birinchisi", callback_data=f"wyr:A:{msg.chat_id}"),
             InlineKeyboardButton("🅱️ Ikkinchisi", callback_data=f"wyr:B:{msg.chat_id}")]
        ])
    )
    _wyr_votes[msg.chat_id][sent.message_id] = {"A": set(), "B": set(), "q": (a, b)}


async def wyr_vote_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, choice, chat_id_str = query.data.split(":")
    chat_id = int(chat_id_str)
    msg_id = query.message.message_id
    user_id = update.effective_user.id

    data = _wyr_votes.get(chat_id, {}).get(msg_id)
    if not data:
        await query.answer("Bu so'rovnoma eskirib qolgan 😅")
        return

    other = "B" if choice == "A" else "A"
    data[other].discard(user_id)
    data[choice].add(user_id)

    a_count = len(data["A"])
    b_count = len(data["B"])
    total = a_count + b_count or 1
    a, b = data["q"]

    text = (
        f"🤔 <b>WOULD YOU RATHER?</b>\n\n"
        f"🅰️ {a}\n"
        f"<code>{'█' * round(a_count/total*10)}{'░' * (10-round(a_count/total*10))}</code> {a_count} ovoz ({round(a_count/total*100)}%)\n\n"
        f"<b>YO'QSA</b>\n\n"
        f"🅱️ {b}\n"
        f"<code>{'█' * round(b_count/total*10)}{'░' * (10-round(b_count/total*10))}</code> {b_count} ovoz ({round(b_count/total*100)}%)"
    )
    await query.answer(f"{'🅰️' if choice=='A' else '🅱️'} tanladi!")
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🅰️ Birinchisi", callback_data=f"wyr:A:{chat_id}"),
         InlineKeyboardButton("🅱️ Ikkinchisi", callback_data=f"wyr:B:{chat_id}")]
    ]))


# ── /8ball ────────────────────────────────────────────────────────────────────
EIGHTBALL_ANSWERS = [
    "🎱 Ha, albatta!", "🎱 Shubhasiz shunday!", "🎱 Menimcha — HA!",
    "🎱 Bugungi kunda — ha.", "🎱 Kelajak yaxshi ko'rinadi!",
    "🎱 Mantiq shunday deydi.", "🎱 Ha, lekin ehtiyot bo'l.",
    "🎱 Biroz vaqt o'tishi kerak... lekin HA.",
    "🎱 Hozircha aniq emas — qayta so'ra.",
    "🎱 Javob loyiq emas hozir.",
    "🎱 Men javob berishdan bosh tortaman.",
    "🎱 Yo'q, bu yaxshi g'oya emas.",
    "🎱 Menimcha — YO'Q.",
    "🎱 Hech qachon bo'lmaydi!", "🎱 Yulduzlar bunга qarshi.",
    "🎱 Umid qilma...", "🎱 Shubhali.", "🎱 Kelajak noaniq.",
]

async def eightball_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    question = " ".join(context.args) if context.args else ""
    if not question:
        await msg.reply_text("❓ Savol bering! Misol: /8ball Bugun omadim bormi?")
        return
    answer = random.choice(EIGHTBALL_ANSWERS)
    await msg.reply_text(
        f"🎱 <b>SEHR SHARI</b>\n\n"
        f"❓ <i>{question}</i>\n\n"
        f"{answer}",
        parse_mode="HTML"
    )


# ── /rps — Tosh-qaychi-qog'oz (Ko'p kishilik + botga qarshi) ─────────────────
RPS_EMOJI = {"tosh": "🪨", "qaychi": "✂️", "qogoz": "📄"}
RPS_BEATS = {"tosh": "qaychi", "qaychi": "qogoz", "qogoz": "tosh"}
RPS_LOBBY_TTL = 300  # 5 daqiqa — javobsiz lobbi shu vaqtdan keyin o'chiriladi

# {chat_id: {game_id: {"players": {uid: choice|None}, "names": {uid: name},
#                       "max": int, "starter": int, "created": float}}}
_rps_lobby: dict[int, dict[str, dict]] = defaultdict(dict)


def _rps_purge_stale(chat_id: int) -> None:
    """RPS_LOBBY_TTL dan eski, hech qachon to'lmagan lobbilarni tozalaydi."""
    now = time.time()
    stale = [
        gid for gid, g in _rps_lobby.get(chat_id, {}).items()
        if now - g["created"] > RPS_LOBBY_TTL
    ]
    for gid in stale:
        del _rps_lobby[chat_id][gid]


async def _rps_safe_edit(query, text: str, reply_markup=None) -> None:
    """edit_message_text — Telegram 'message is not modified' xatosini yutadi."""
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


def _rps_status_text(game: dict) -> str:
    joined = len(game["players"])
    max_p = game["max"]
    lines = [f"🪨✂️📄 <b>TOSH-QAYCHI-QO'G'OZ</b>  ({joined}/{max_p} o'yinchi)\n"]
    for uid, choice in game["players"].items():
        name = game["names"][uid]
        status = "✅ Tanladi" if choice else "⏳ Kutmoqda..."
        lines.append(f"• {name} — {status}")
    ready = "▶️ Hamma tayyor!" if joined == max_p and all(game["players"].values()) else "👇 Qo'shiling va tanlang!"
    lines.append(f"\n{ready}")
    return "\n".join(lines)


def _rps_keyboard(game_id: str, starter_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🪨 Tosh",   callback_data=f"rps:pick:tosh:{game_id}"),
            InlineKeyboardButton("✂️ Qaychi", callback_data=f"rps:pick:qaychi:{game_id}"),
            InlineKeyboardButton("📄 Qog'oz", callback_data=f"rps:pick:qogoz:{game_id}"),
        ],
        [
            InlineKeyboardButton("➕ Qo'shilish", callback_data=f"rps:join:{game_id}"),
            InlineKeyboardButton("❌ Bekor qilish", callback_data=f"rps:cancel:{game_id}:{starter_id}"),
        ]
    ])


def _rps_resolve(game: dict) -> str:
    """O'yin natijasini hisoblaydi."""
    players = game["players"]  # {uid: choice}
    names = game["names"]
    choices = list(players.values())
    uids = list(players.keys())

    # Hammaning tanlovi bir xil → durrang
    if len(set(choices)) == 1:
        return "🤝 <b>DURRANG!</b> Hamma bir xil tanladi!"

    # G'oliblarni topish
    winners = []
    losers = []
    for uid, choice in players.items():
        beats_someone = any(
            RPS_BEATS[choice] == other_choice
            for other_uid, other_choice in players.items()
            if other_uid != uid
        )
        beaten_by_someone = any(
            RPS_BEATS[other_choice] == choice
            for other_uid, other_choice in players.items()
            if other_uid != uid
        )
        if beats_someone and not beaten_by_someone:
            winners.append(uid)
        elif beaten_by_someone and not beats_someone:
            losers.append(uid)

    if not winners:
        return "🤝 <b>DURRANG!</b> Hech kim yutmadi!"

    lines = ["🏆 <b>NATIJALAR:</b>\n"]
    for uid, choice in players.items():
        em = RPS_EMOJI[choice]
        name = names[uid]
        if uid in winners:
            lines.append(f"🥇 {name}: {em} {choice} — <b>YUTDI!</b>")
        elif uid in losers:
            lines.append(f"💀 {name}: {em} {choice} — yutqazdi")
        else:
            lines.append(f"🤝 {name}: {em} {choice} — durrang")
    return "\n".join(lines)


async def rps_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if await _group_only(msg): return

    _rps_purge_stale(msg.chat_id)

    # Nechta kishilik tanlash (botga qarshi yakka o'yin ham bor)
    await msg.reply_text(
        "🪨✂️📄 <b>TOSH-QAYCHI-QO'G'OZ</b>\n\nNechta kishilik o'yin?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🤖 Botga qarshi", callback_data=f"rps:bot:{msg.from_user.id}"),
        ],[
            InlineKeyboardButton("👤👤 2 kishilik", callback_data=f"rps:new:2:{msg.from_user.id}"),
            InlineKeyboardButton("👤👤👤 3 kishilik", callback_data=f"rps:new:3:{msg.from_user.id}"),
        ],[
            InlineKeyboardButton("4 kishilik", callback_data=f"rps:new:4:{msg.from_user.id}"),
            InlineKeyboardButton("5 kishilik", callback_data=f"rps:new:5:{msg.from_user.id}"),
        ]])
    )


async def rps_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    chat_id = query.message.chat_id
    parts = query.data.split(":")
    action = parts[1]

    # ── Botga qarshi yakka o'yin ─────────────────────────────────────────────
    if action == "bot":
        starter_id = int(parts[2])
        if user.id != starter_id:
            await query.answer("Bu sizning o'yiningiz emas! 😅", show_alert=True)
            return
        await query.answer()
        await query.edit_message_text(
            "🤖 <b>BOTGA QARSHI</b>\n\nTanlovingizni qiling 👇",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🪨 Tosh",   callback_data=f"rps:vsbot:tosh:{starter_id}"),
                InlineKeyboardButton("✂️ Qaychi", callback_data=f"rps:vsbot:qaychi:{starter_id}"),
                InlineKeyboardButton("📄 Qog'oz", callback_data=f"rps:vsbot:qogoz:{starter_id}"),
            ]])
        )
        return

    if action == "vsbot":
        choice = parts[2]
        starter_id = int(parts[3])
        if user.id != starter_id:
            await query.answer("Bu sizning o'yiningiz emas! 😅", show_alert=True)
            return
        bot_choice = random.choice(list(RPS_EMOJI))
        if choice == bot_choice:
            verdict = "🤝 <b>DURRANG!</b>"
        elif RPS_BEATS[choice] == bot_choice:
            verdict = "🏆 <b>SIZ YUTDINGIZ!</b>"
        else:
            verdict = "💀 <b>BOT YUTDI!</b>"
        await query.answer(f"{RPS_EMOJI[choice]} vs {RPS_EMOJI[bot_choice]}")
        await query.edit_message_text(
            f"🤖 <b>BOTGA QARSHI — NATIJA</b>\n\n"
            f"Siz: {RPS_EMOJI[choice]} {choice}\n"
            f"Bot: {RPS_EMOJI[bot_choice]} {bot_choice}\n\n{verdict}",
            parse_mode="HTML"
        )
        return

    # ── Yangi ko'p kishilik o'yin yaratish ───────────────────────────────────
    if action == "new":
        max_p = int(parts[2])
        starter_id = int(parts[3])
        if user.id != starter_id:
            await query.answer("Faqat boshlagan kishi tanlaydi! 😅", show_alert=True)
            return

        _rps_purge_stale(chat_id)
        import uuid as _uuid
        game_id = _uuid.uuid4().hex[:8]
        game = {
            "players": {user.id: None},
            "names": {user.id: user.first_name or str(user.id)},
            "max": max_p,
            "starter": user.id,
            "created": time.time(),
        }
        _rps_lobby[chat_id][game_id] = game

        await _rps_safe_edit(query, _rps_status_text(game), _rps_keyboard(game_id, user.id))
        await query.answer("O'yin yaratildi! Do'stlarni kutmoqda...")

    # ── O'yinni bekor qilish ─────────────────────────────────────────────────
    elif action == "cancel":
        game_id, starter_id = parts[2], int(parts[3])
        game = _rps_lobby.get(chat_id, {}).get(game_id)
        if not game:
            await query.answer("O'yin allaqachon tugagan!", show_alert=True)
            return
        if user.id != game["starter"]:
            await query.answer("Faqat o'yin boshlovchisi bekor qila oladi! 😅", show_alert=True)
            return
        del _rps_lobby[chat_id][game_id]
        await _rps_safe_edit(query, "❌ <b>O'yin bekor qilindi.</b>")
        await query.answer("Bekor qilindi.")

    # ── O'yinga qo'shilish ────────────────────────────────────────────────────
    elif action == "join":
        game_id = parts[2]
        game = _rps_lobby.get(chat_id, {}).get(game_id)
        if not game:
            await query.answer("Bu o'yin topilmadi yoki tugagan! 😅", show_alert=True)
            return
        if user.id in game["players"]:
            await query.answer("Siz allaqachon o'yindasiz! Tanlovingizni qiling 👇", show_alert=True)
            return
        if len(game["players"]) >= game["max"]:
            await query.answer("O'yin to'ldi! Keyingi safar 😅", show_alert=True)
            return

        game["players"][user.id] = None
        game["names"][user.id] = user.first_name or str(user.id)
        await _rps_safe_edit(query, _rps_status_text(game), _rps_keyboard(game_id, game["starter"]))
        await query.answer("Qo'shildingiz! Endi tanlang 👆")

    # ── Tanlov qilish ─────────────────────────────────────────────────────────
    elif action == "pick":
        choice = parts[2]
        game_id = parts[3]
        game = _rps_lobby.get(chat_id, {}).get(game_id)
        if not game:
            await query.answer("O'yin topilmadi! 😅", show_alert=True)
            return
        if user.id not in game["players"]:
            await query.answer("Siz bu o'yinda emassiz! ➕ bosing.", show_alert=True)
            return

        game["players"][user.id] = choice
        await query.answer(f"{RPS_EMOJI[choice]} Tanladingiz! Boshqalarni kutmoqda...")

        # Hamma tanlovi qilganmi?
        if all(game["players"].values()) and len(game["players"]) == game["max"]:
            result = _rps_resolve(game)
            details = "\n\n📋 <b>Barcha tanlovlar:</b>\n"
            for uid, ch in game["players"].items():
                details += f"  {game['names'][uid]}: {RPS_EMOJI[ch]} {ch}\n"
            await _rps_safe_edit(query, f"🪨✂️📄 <b>O'YIN TUGADI!</b>\n\n{result}{details}")
            del _rps_lobby[chat_id][game_id]
        else:
            await _rps_safe_edit(query, _rps_status_text(game), _rps_keyboard(game_id, game["starter"]))

    # ── Eski / noma'lum format (fallback) ────────────────────────────────────
    else:
        await query.answer("Eski format, /rps qaytadan bosing!", show_alert=True)


# ═══════════════════════════════════════════════════════════════════════════════
# 🎲 TASODIF / FUN
# ═══════════════════════════════════════════════════════════════════════════════

# ── /roast ────────────────────────────────────────────────────────────────────
async def roast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if await _group_only(msg): return
    uid, name, username = _get_mention_user(msg)
    if not name:
        await msg.reply_text("❌ /roast @foydalanuvchi yoki reply qiling!")
        return
    tag = _mention(uid, name, username) if uid else name
    roast = _ai(
        "Sen Misumi AI — Telegram guruhidagi eng hazilkash, tili achchiq "
        "do'stsan. Foydalanuvchi haqida DO'STONA va KULGILI (haqiqiy "
        "haqorat, kamsitish yoki og'ir gap emas) 'roast' yoz — xuddi "
        "yaqin do'stlar bir-birini guruh chatida qiyqirtirib kulganday. "
        "O'zbekcha jonli, kundalik uslubda (jargon/slang ishlatsang "
        "bo'ladi), 1-3 jumla, har safar boshqacha va o'ziga xos chiqsin — "
        "shablon gapni takrorlama. FAQAT roast matnini yoz.",
        f"Roast qilinayotgan kishi: {name}",
        f"Vay {name}, bu guruhda seni ko'rganda hamma qo'lini yuziga bosadi... lekin ko'rib turibdi! 😄"
    )
    await msg.reply_text(
        f"🔥 <b>ROAST</b> 🔥\n\n{tag}!\n\n{roast}",
        parse_mode="HTML"
    )


# ── /compliment ───────────────────────────────────────────────────────────────
async def compliment_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid, name, username = _get_mention_user(msg)
    if not name:
        name = update.effective_user.first_name
        uid = update.effective_user.id
        username = update.effective_user.username
    tag = _mention(uid, name, username) if uid else name
    comp = _ai(
        "Sen Misumi AI — Telegram guruhidagi mehribon botsan. "
        "Foydalanuvchi haqida CHIN DILDAN va CHIROYLI maqtov yoz. "
        "O'zbek tilida, 2-3 jumla, iliq va samimiy. FAQAT maqtov matnini yoz.",
        f"Maqtalayotgan kishi: {name}",
        f"{name} — bu guruhning eng yaxshi odamlaridan biri! Har doim kulgisi, mehnati bilan hammasini bezab turadi 💫"
    )
    await msg.reply_text(
        f"✨ <b>MAQTOV</b> ✨\n\n{tag}!\n\n{comp}\n\n💖",
        parse_mode="HTML"
    )


# ── /horoscope ────────────────────────────────────────────────────────────────
async def horoscope_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user
    zodiac = random.choice(ZODIAC_SIGNS)
    today = datetime.now().strftime("%d.%m.%Y")
    horoscope = _ai(
        "Sen yulduz burji mutaxassisisisan. "
        "Foydalanuvchi uchun bugungi yulduz burji bashoratini yoz. "
        "O'zbek tilida, 3-4 jumla, romantik va umidvor uslub. Sevgi, ish va sog'liq haqida. "
        "FAQAT bashorat matnini yoz.",
        f"Burj: {zodiac}\nSana: {today}\nFoydalanuvchi: {user.first_name}",
        f"Bugun {user.first_name} uchun yulduzlar yaxshi energiya yubormoqda! Sevgida omad, ishda muvaffaqiyat kutmoqda 🌟"
    )
    await msg.reply_text(
        f"🔮 <b>BUGUNGI BURJ BASHORATI</b>\n"
        f"📅 {today}\n\n"
        f"{zodiac}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{horoscope}\n\n"
        f"⭐ <i>Yulduzlar hech qachon yolg'on gapirmaydi!</i>",
        parse_mode="HTML"
    )


# ── /lucky ────────────────────────────────────────────────────────────────────
LUCKY_COLORS = [
    "🔴 Qizil", "🟠 To'q sariq", "🟡 Sariq", "🟢 Yashil",
    "🔵 Ko'k", "🟣 Binafsha", "⚫ Qora", "⚪ Oq", "🟤 Jigarrang"
]
LUCKY_ACTIVITIES = [
    "yangi do'st orttirish", "sevgilisi bilan uchrashish", "pul topish",
    "yaxshi yangilik eshitish", "ekzamenda 'A' olish", "omadli sovg'a topish",
    "qo'shni bilan muzlatki taqsimlash 😄", "yangi loyiha boshlash"
]

async def lucky_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user
    lucky_num = random.randint(1, 99)
    lucky_color = random.choice(LUCKY_COLORS)
    lucky_act = random.choice(LUCKY_ACTIVITIES)
    lucky_hour = f"{random.randint(8, 22)}:00"
    luck_pct = random.randint(40, 99)
    await msg.reply_text(
        f"🍀 <b>BUGUNGI OMAD</b> — {user.first_name}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔢 Omad raqami: <b>{lucky_num}</b>\n"
        f"🎨 Omad rangi: <b>{lucky_color}</b>\n"
        f"⏰ Eng omadli soat: <b>{lucky_hour}</b>\n"
        f"✨ Bugun omad kutilgan narsa: <b>{lucky_act}</b>\n\n"
        f"📊 Bugungi omad darajasi: <b>{luck_pct}%</b>\n"
        f"{'⭐' * round(luck_pct/20)}",
        parse_mode="HTML"
    )


# ── /rate ─────────────────────────────────────────────────────────────────────
RATE_CATEGORIES = [
    ("🧠 Aqlli", "aqlli"),
    ("💪 Kuchli", "kuchli"),
    ("😍 Chiroyli", "chiroyli"),
    ("😂 Kulgili", "kulgili"),
    ("❤️ Mehribon", "mehribon"),
    ("🔥 Trend", "trend"),
    ("😴 Uyquchan", "uyquchan"),
    ("🍕 Pizzani sevadi", "pizza sevish"),
]

async def rate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid, name, username = _get_mention_user(msg)
    if not name:
        name = update.effective_user.first_name
        uid = update.effective_user.id
        username = update.effective_user.username
    tag = _mention(uid, name, username) if uid else name

    cats = random.sample(RATE_CATEGORIES, 4)
    lines = [f"📊 <b>{name} REYTINGI</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"]
    for emoji_label, _ in cats:
        pct = random.randint(10, 99)
        bar = "█" * round(pct/20) + "░" * (5 - round(pct/20))
        lines.append(f"{emoji_label}: {bar} <b>{pct}%</b>")
    await msg.reply_text(
        "\n".join(lines) + f"\n━━━━━━━━━━━━━━━━━━━━━━━\n{tag} — mana shu! 😄",
        parse_mode="HTML"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 💬 GURUH HAYOTI
# ═══════════════════════════════════════════════════════════════════════════════

# ── /quote ────────────────────────────────────────────────────────────────────
async def quote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    topic = " ".join(context.args) if context.args else ""
    topic_hint = f"mavzu: {topic}" if topic else "istalgan mavzu"
    quote = _ai(
        "Sen hikmatlı so'zlar ustasisisan. "
        "BITTA qisqa va chuqur motivatsion gap yoz (1-2 jumla, o'zbek tilida). "
        "Original, ijodiy, qalbga teguvchi bo'lsin. FAQAT iqtibos matnini yoz.",
        f"Mavzu: {topic_hint}",
        "Har bir kun yangi imkoniyat — faqat ko'zing ochiq bo'lsin! 🌅"
    )
    author = _ai(
        "O'zbek tilida qisqa (2-4 so'z) tasavvuriy muallif ismi o'ylab top. "
        "Real odam emas, xayoliy. FAQAT ismni yoz.",
        "muallif ismi", "Misumi Donishmand"
    )
    await msg.reply_text(
        f"💭 <i>«{quote}»</i>\n\n— <b>{author}</b>",
        parse_mode="HTML"
    )


# ── /confession ───────────────────────────────────────────────────────────────
async def confession_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if await _group_only(msg): return
    confession = _ai(
        "Sen Telegram guruh botisan. "
        "Guruh a'zosidan anonim e'tirof (confession) — qiziq, hazilomuz yoki romantik. "
        "O'zbek tilida, 2-3 jumla, sirli uslubda. FAQAT e'tirof matnini yoz.",
        "guruh anonim e'tirof",
        "Bu guruhda bir kishi bor... har safar u xabar yozganda, mening yuragim tezroq uradi. Kim ekanini bilmang... 🤫"
    )
    await msg.reply_text(
        f"🤫 <b>ANONIM E'TIROF</b>\n\n"
        f"<i>«{confession}»</i>\n\n"
        f"— <b>Noma'lum guruh a'zosi</b>",
        parse_mode="HTML"
    )


# ── /ngl ──────────────────────────────────────────────────────────────────────
async def ngl_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if await _group_only(msg): return
    members = dict(_seen_members.get(msg.chat_id, {}))
    if not members:
        await msg.reply_text("❌ Guruhda hali a'zolar yo'q! Avval xabar yuboring.")
        return
    uid, mdata = random.choice(list(members.items()))
    name = mdata["name"]
    ngl = _ai(
        "Sen Telegram guruh botisan. "
        f"{name} haqida 'NGL (Not Gonna Lie)' — rostini aytganda — qiziq, hazilomuz gap yoz. "
        "O'zbek tilida, 1-2 jumla. FAQAT gap matnini yoz.",
        f"Kishi: {name}",
        f"NGL, {name} bu guruhning eng qiziq odamlaridan biri — har safar bir yangilik bor! 😄"
    )
    tag = _mention(uid, name, mdata.get("username"))
    await msg.reply_text(
        f"💬 <b>NGL...</b>\n\n"
        f"{tag} haqida: <i>{ngl}</i>",
        parse_mode="HTML"
    )


# ── /fakechat ─────────────────────────────────────────────────────────────────
async def fakechat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if await _group_only(msg): return
    members = dict(_seen_members.get(msg.chat_id, {}))
    if len(members) < 2:
        await msg.reply_text("❌ Kamida 2 ta a'zo kerak!")
        return
    ids = random.sample(list(members.keys()), 2)
    m1, m2 = members[ids[0]], members[ids[1]]
    n1, n2 = m1["name"], m2["name"]
    fakechat = _ai(
        "Sen Telegram guruh botisan. "
        f"{n1} va {n2} o'rtasida HAYOLIY va KULGILI suhbat yoz. "
        "O'zbek tilida, 4-6 xabar (almashinuv), hazil va qiziqarli. "
        "Format: 'Ism: xabar' ko'rinishida. FAQAT suhbatni yoz.",
        f"Suhbatdoshlar: {n1} va {n2}",
        f"{n1}: Salom! Bugun nima qilding?\n{n2}: Uyda o'tirdim, sen-chi?\n{n1}: Men ham... biz ikkalomiz ham bir xilmiz 😄"
    )
    await msg.reply_text(
        f"📱 <b>HAYOLIY SUHBAT</b>\n"
        f"👤 {n1}  🤝  {n2}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<i>{fakechat}</i>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Bu suhbat to'liq hayoliy! 😄</i>",
        parse_mode="HTML"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 📊 STATISTIKA
# ═══════════════════════════════════════════════════════════════════════════════

# ── /stats ────────────────────────────────────────────────────────────────────
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if await _group_only(msg): return
    chat_id = msg.chat_id
    counts = _msg_count.get(chat_id, {})
    members = _seen_members.get(chat_id, {})
    total = sum(counts.values())
    top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:5]
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    lines = [f"📊 <b>GURUH STATISTIKASI</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"]
    lines.append(f"👥 Kuzatilgan a'zolar: <b>{len(members)}</b>")
    lines.append(f"💬 Jami xabarlar: <b>{total}</b>\n")
    if top:
        lines.append("🏆 <b>TOP FAOLLAR:</b>")
        for i, (uid, cnt) in enumerate(top):
            name = members.get(uid, {}).get("name", str(uid))
            tag = _mention(uid, name, members.get(uid, {}).get("username"))
            lines.append(f"{medals[i]} {tag} — {cnt} ta xabar")
    await msg.reply_text("\n".join(lines), parse_mode="HTML")


# ── /who ──────────────────────────────────────────────────────────────────────
async def who_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if await _group_only(msg): return
    chat_id = msg.chat_id
    counts = _msg_count.get(chat_id, {})
    members = _seen_members.get(chat_id, {})
    if not counts:
        await msg.reply_text("❌ Hali statistika yo'q!")
        return
    top_uid = max(counts, key=counts.get)
    name = members.get(top_uid, {}).get("name", str(top_uid))
    tag = _mention(top_uid, name, members.get(top_uid, {}).get("username"))
    await msg.reply_text(
        f"🏆 <b>ENG FAOL A'ZO</b>\n\n"
        f"{tag}\n"
        f"💬 <b>{counts[top_uid]}</b> ta xabar yuborgan!\n\n"
        f"<i>Bu guruhning yuragi! 💪</i>",
        parse_mode="HTML"
    )


# ── /activity ─────────────────────────────────────────────────────────────────
async def activity_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if await _group_only(msg): return
    chat_id = msg.chat_id
    lines = [f"📈 <b>OXIRGI 7 KUNDAGI FAOLLIK</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"]
    has_data = False
    for i in range(6, -1, -1):
        day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        display = (datetime.now() - timedelta(days=i)).strftime("%d.%m")
        day_counts = _daily_count.get(chat_id, {}).get(day, {})
        total = sum(day_counts.values())
        if total > 0:
            has_data = True
        bar = "█" * min(total, 20) if total > 0 else "░"
        lines.append(f"📅 {display}: {bar} <b>{total}</b>")
    if not has_data:
        lines.append("\n<i>Hali yetarli ma'lumot yo'q!</i>")
    await msg.reply_text("\n".join(lines), parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════════════════
# 🌍 FOYDALI
# ═══════════════════════════════════════════════════════════════════════════════

# ── /translate ────────────────────────────────────────────────────────────────
async def translate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    # Reply xabarni tarjima qilish
    target_text = ""
    if msg.reply_to_message and msg.reply_to_message.text:
        target_text = msg.reply_to_message.text
        lang = " ".join(context.args) if context.args else "o'zbek"
    elif context.args:
        lang_and_text = " ".join(context.args)
        # "/translate en Salom dunyo" formatini ko'rib chiqamiz
        parts = lang_and_text.split(" ", 1)
        if len(parts) == 2 and len(parts[0]) <= 10:
            lang, target_text = parts
        else:
            lang = "ingliz"
            target_text = lang_and_text
    else:
        await msg.reply_text(
            "❓ <b>Foydalanish:</b>\n"
            "• Xabarga reply: /translate <til>\n"
            "• To'g'ridan: /translate <til> <matn>\n\n"
            "Misol: /translate ingliz Salom dunyo",
            parse_mode="HTML"
        )
        return

    translation = _ai(
        f"Sen tarjimon botsan. Matnni {lang} tiliga tarjima qil. "
        f"FAQAT tarjima matnini yoz, boshqa hech narsa qo'shma.",
        f"Matn: {target_text}",
        target_text
    )
    await msg.reply_text(
        f"🌍 <b>TARJIMA</b> → <b>{lang}</b>\n\n"
        f"<i>{translation}</i>",
        parse_mode="HTML"
    )


# ── /weather ──────────────────────────────────────────────────────────────────
async def weather_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    city = " ".join(context.args) if context.args else ""
    if not city:
        await msg.reply_text("❓ Shahar nomini kiriting! Misol: /weather Toshkent")
        return
    try:
        # wttr.in API (API key shart emas)
        resp = requests.get(f"https://wttr.in/{city}?format=j1", timeout=7)
        if resp.ok:
            data = resp.json()
            current = data["current_condition"][0]
            temp_c = current["temp_C"]
            feels_like = current["FeelsLikeC"]
            humidity = current["humidity"]
            desc = current["weatherDesc"][0]["value"]
            wind = current["windspeedKmph"]
            emojis = {"sunny": "☀️", "cloud": "☁️", "rain": "🌧️", "snow": "❄️", "overcast": "⛅"}
            em = "🌤️"
            for k, v in emojis.items():
                if k.lower() in desc.lower():
                    em = v
                    break
            await msg.reply_text(
                f"{em} <b>OB-HAVO — {city.upper()}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🌡 Harorat: <b>{temp_c}°C</b> (seziladi: {feels_like}°C)\n"
                f"💧 Namlik: <b>{humidity}%</b>\n"
                f"💨 Shamol: <b>{wind} km/soat</b>\n"
                f"📋 Holat: <b>{desc}</b>",
                parse_mode="HTML"
            )
        else:
            await msg.reply_text(f"❌ '{city}' shahri topilmadi. Tekshiring!")
    except Exception as e:
        await msg.reply_text(f"❌ Ob-havo ma'lumotini olishda xatolik: {e}")


# ── /calc ─────────────────────────────────────────────────────────────────────
async def calc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    expr = " ".join(context.args) if context.args else ""
    if not expr:
        await msg.reply_text("❓ Misol kiriting! Masalan: /calc 25 * 4 + 10")
        return
    try:
        # Xavfsiz hisoblash (faqat raqamlar va amallar)
        allowed = set("0123456789+-*/.() ")
        if not all(c in allowed for c in expr):
            raise ValueError("Noto'g'ri belgilar")
        result = eval(expr, {"__builtins__": {}}, {"sqrt": math.sqrt, "pi": math.pi})
        await msg.reply_text(
            f"🧮 <b>KALKULYATOR</b>\n\n"
            f"📝 {expr}\n"
            f"= <b>{result}</b>",
            parse_mode="HTML"
        )
    except Exception:
        await msg.reply_text(f"❌ Hisoblashda xatolik! Misol: /calc 25 * 4 + 10")


# ── /remind ───────────────────────────────────────────────────────────────────
async def remind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    # Format: /remind 5 daqiqa xabar matni
    args = context.args
    if not args or len(args) < 3:
        await msg.reply_text(
            "❓ <b>Foydalanish:</b> /remind <vaqt> <birlik> <xabar>\n\n"
            "Birlik: daqiqa, soat\n"
            "Misol: /remind 5 daqiqa Ovqat yeyish vaqti!",
            parse_mode="HTML"
        )
        return
    try:
        amount = int(args[0])
        unit = args[1].lower()
        reminder_text = " ".join(args[2:])
        if unit in ("daqiqa", "min", "minute"):
            seconds = amount * 60
        elif unit in ("soat", "hour"):
            seconds = amount * 3600
        else:
            await msg.reply_text("❌ Birlik: 'daqiqa' yoki 'soat' bo'lishi kerak!")
            return
        if seconds > 86400:
            await msg.reply_text("❌ Maksimum 24 soat!")
            return
        await msg.reply_text(
            f"⏰ <b>ESLATMA O'RNATILDI</b>\n\n"
            f"⏱ Vaqt: <b>{amount} {unit}</b> keyin\n"
            f"📝 Xabar: <i>{reminder_text}</i>",
            parse_mode="HTML"
        )
        chat_id = msg.chat_id
        user_id = update.effective_user.id
        user_name = update.effective_user.first_name
        tag = _mention(user_id, user_name, update.effective_user.username)

        async def _send_reminder():
            await asyncio.sleep(seconds)
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⏰ <b>ESLATMA!</b>\n\n{tag}\n\n<i>{reminder_text}</i>",
                    parse_mode="HTML"
                )
            except Exception as e:
                print(f"[remind] {e}")

        asyncio.create_task(_send_reminder())
    except ValueError:
        await msg.reply_text("❌ Vaqt raqam bo'lishi kerak! Misol: /remind 5 daqiqa ...")


# ═══════════════════════════════════════════════════════════════════════════════
# 💔 SHIP QO'SHIMCHALARI
# ═══════════════════════════════════════════════════════════════════════════════

# ── /couple — /erxotin inglizcha ─────────────────────────────────────────────
async def couple_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xuddi /erxotin — inglizcha nomi."""
    try:
        from ship import erxotin_cmd
        await erxotin_cmd(update, context)
    except Exception:
        await update.message.reply_text("❌ /couple ishlamadi. /erxotin ni sinab ko'ring!")


# ── /divorced ─────────────────────────────────────────────────────────────────
PROPERTY_ITEMS = [
    "eski divan 🛋️", "buzilib qolgan televizor 📺", "yarim qolgan parfyum 🧴",
    "shared Netflix parol 📱", "3 ta qoshiq 🥄", "to'y torining qoldig'i 🎂",
    "it (oti: Pishti) 🐕", "mushuk (oti: Shirinoy) 🐈", "eski foto album 📷",
    "kir yuvish mashinasi 🫧", "balkon o'simliklarida gul 🌱", "eski kredit 💳",
]

async def divorced_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if await _group_only(msg): return
    members = dict(_seen_members.get(msg.chat_id, {}))
    if len(members) < 2:
        await msg.reply_text("❌ Kamida 2 ta a'zo kerak!")
        return
    ids = random.sample(list(members.keys()), 2)
    m1, m2 = members[ids[0]], members[ids[1]]
    n1, n2 = m1["name"], m2["name"]
    t1 = _mention(ids[0], n1, m1.get("username"))
    t2 = _mention(ids[1], n2, m2.get("username"))

    props = random.sample(PROPERTY_ITEMS, 4)
    caption = _ai(
        "Sen Telegram guruh botisan. "
        "Ikki kishining KULGILI ajrashish e'lonini yoz. "
        "O'zbek tilida, 2-3 jumla, hazil-mutoyiba uslubida. FAQAT izoh matnini yoz.",
        f"Ajrashuvchilar: {n1} va {n2}",
        f"{n1} va {n2} axiyri ajrashishdi. Ikki yil edi... bunga shu guruh guvoh! 💔"
    )

    await msg.reply_text(
        f"💔 <b>AJRASHISH E'LONI</b> 💔\n\n"
        f"👤 {t1}  ➕  {t2}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 MOL-MULK TAQSIMOTI:\n\n"
        f"  {n1} oladi: {props[0]}, {props[1]}\n"
        f"  {n2} oladi: {props[2]}, {props[3]}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💬 <i>{caption}</i>\n\n"
        f"😢 <i>Guruh a'zolari buni ko'rib qayg'urdi...</i>",
        parse_mode="HTML"
    )


# ── /crush ────────────────────────────────────────────────────────────────────
async def crush_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if await _group_only(msg): return
    uid, name, username = _get_mention_user(msg)
    if not name:
        await msg.reply_text("❓ /crush @foydalanuvchi yoki reply qiling!")
        return
    tag = _mention(uid, name, username) if uid else name
    confession = _ai(
        "Sen Telegram guruh botisan. "
        f"{name} ga anonim oshiq bo'lib, yashirin sevgi e'tirof yoz. "
        "O'zbek tilida, 2-3 jumla, romantik va sirin. FAQAT e'tirof matnini yoz.",
        f"Sevgi izhor qilinadigan kishi: {name}",
        f"Meni bilmaysiz, lekin sizni har kun ko'rganimda yuragim tezroq uradi... 🤫"
    )
    await msg.reply_text(
        f"🤫 <b>YASHIRIN SEVGI E'TIROFI</b>\n\n"
        f"💌 {tag} ga:\n\n"
        f"<i>«{confession}»</i>\n\n"
        f"— <b>Noma'lum oshiq 💘</b>",
        parse_mode="HTML"
    )


# ── /soulmate ─────────────────────────────────────────────────────────────────
async def soulmate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if await _group_only(msg): return
    members = dict(_seen_members.get(msg.chat_id, {}))
    user = update.effective_user
    if len(members) < 2:
        await msg.reply_text("❌ Kamida 2 ta a'zo kerak!")
        return
    # O'zidan boshqa tasodifiy
    others = {k: v for k, v in members.items() if k != user.id}
    if not others:
        await msg.reply_text("❌ Kamida 2 ta boshqa a'zo kerak!")
        return
    uid, mdata = random.choice(list(others.items()))
    name = mdata["name"]
    tag = _mention(uid, name, mdata.get("username"))
    user_tag = _mention(user.id, user.first_name, user.username)

    compat = random.randint(85, 99)
    past_life = _ai(
        "Sen mistik ruhiy sherik topuvchisan. "
        "Ikki kishining o'tgan hayotdagi munosabatini va telepatik aloqasini qisqacha tasvirla. "
        "O'zbek tilida, 2-3 jumla, mistik va romantik. FAQAT matnni yoz.",
        f"Sheriklar: {user.first_name} va {name}",
        f"Bu ikki ruh ming yil avval ham bir-birini uchratgan. Ularning o'rtasidagi aloqa hech qachon o'chmaydi 🌟"
    )
    await msg.reply_text(
        f"🔮 <b>RUHIY SHERIK</b> 🔮\n\n"
        f"✨ {user_tag}  🤝  {tag}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💫 Telepatik mos kelish: <b>{compat}%</b>\n"
        f"{'⭐' * round(compat/20)}\n\n"
        f"🌌 <i>{past_life}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Qismat bu juftni ming yil avval tanlagan!</i>",
        parse_mode="HTML"
    )


# ── /exship ───────────────────────────────────────────────────────────────────
async def exship_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if await _group_only(msg): return
    members = dict(_seen_members.get(msg.chat_id, {}))
    if len(members) < 2:
        await msg.reply_text("❌ Kamida 2 ta a'zo kerak!")
        return
    ids = random.sample(list(members.keys()), 2)
    m1, m2 = members[ids[0]], members[ids[1]]
    n1, n2 = m1["name"], m2["name"]
    t1 = _mention(ids[0], n1, m1.get("username"))
    t2 = _mention(ids[1], n2, m2.get("username"))

    days_together = random.randint(14, 365)
    breakup_reasons = [
        "telefon parolini aytmagan 📱", "kino tanlovida kelisha olmagan 🎬",
        "pizza ustida tortishuv 🍕", "uxlash vaqti bo'yicha ixtilof 😴",
        "'ok' deb javob bergan 💬", "emoji noto'g'ri ishlatgan 🙃",
        "do'stlari bilan ko'proq bo'lgan 👥", "eski sevgilisi IG'ni kuzatgan 👀",
    ]
    reason = random.choice(breakup_reasons)
    drama = _ai(
        "Sen Telegram guruh botisan. "
        f"{n1} va {n2} ajrashganini KULGILI va DRAMATIK uslubda e'lon qil. "
        "O'zbek tilida, 2-3 jumla. FAQAT matnni yoz.",
        f"Eski juft: {n1} va {n2}",
        f"Ular ajrashdi... guruh hech qachon bir xil bo'lmaydi endi 💔"
    )
    await msg.reply_text(
        f"💔 <b>ESKI JUFT — TARIX</b> 💔\n\n"
        f"👤 {t1}  ✖️  {t2}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 Birga bo'lgan: <b>{days_together} kun</b>\n"
        f"💥 Ajrashish sababi: <b>{reason}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📖 <i>{drama}</i>\n\n"
        f"<i>RIP bu juft... 😢</i>",
        parse_mode="HTML"
    )


# ── /husband / /wife ──────────────────────────────────────────────────────────
async def husband_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if await _group_only(msg): return
    uid, name, username = _get_mention_user(msg)
    if not name:
        await msg.reply_text("❓ /husband @foydalanuvchi yoki reply qiling!")
        return
    males = _members_by_gender(msg.chat_id, "erkak", exclude={uid})
    gender_note = ""
    if males:
        hid, hdata = random.choice(list(males.items()))
    else:
        members = dict(_seen_members.get(msg.chat_id, {}))
        others = {k: v for k, v in members.items() if k != uid}
        if not others:
            await msg.reply_text("❌ Kamida 2 ta a'zo kerak!")
            return
        hid, hdata = random.choice(list(others.items()))
        gender_note = (
            "\n\n⚠️ <i>Erkak deb belgilangan a'zo topilmadi — tasodifiy tanlandi. "
            "<code>/jins erkak</code> yozib jinsingizni belgilang.</i>"
        )
    h_tag = _mention(hid, hdata["name"], hdata.get("username"))
    target_tag = _mention(uid, name, username) if uid else name
    await msg.reply_text(
        f"💍 <b>{name} ning ERI</b>\n\n"
        f"👰 {target_tag}\n"
        f"👨 {h_tag}\n\n"
        f"<i>Guruh guvoh bo'ldi! 🎊</i>{gender_note}",
        parse_mode="HTML"
    )


async def wife_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if await _group_only(msg): return
    uid, name, username = _get_mention_user(msg)
    if not name:
        await msg.reply_text("❓ /wife @foydalanuvchi yoki reply qiling!")
        return
    females = _members_by_gender(msg.chat_id, "ayol", exclude={uid})
    gender_note = ""
    if females:
        wid, wdata = random.choice(list(females.items()))
    else:
        members = dict(_seen_members.get(msg.chat_id, {}))
        others = {k: v for k, v in members.items() if k != uid}
        if not others:
            await msg.reply_text("❌ Kamida 2 ta a'zo kerak!")
            return
        wid, wdata = random.choice(list(others.items()))
        gender_note = (
            "\n\n⚠️ <i>Ayol deb belgilangan a'zo topilmadi — tasodifiy tanlandi. "
            "<code>/jins ayol</code> yozib jinsingizni belgilang.</i>"
        )
    w_tag = _mention(wid, wdata["name"], wdata.get("username"))
    target_tag = _mention(uid, name, username) if uid else name
    await msg.reply_text(
        f"💍 <b>{name} ning XOTINI</b>\n\n"
        f"👨 {target_tag}\n"
        f"👰 {w_tag}\n\n"
        f"<i>Guruh guvoh bo'ldi! 🎊</i>{gender_note}",
        parse_mode="HTML"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 📊 Xabar kuzatuvchi
# ═══════════════════════════════════════════════════════════════════════════════

async def _track_fun_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not update.effective_chat:
        return
    if update.effective_chat.type not in ("group", "supergroup"):
        return
    user = update.effective_user
    if not user or user.is_bot:
        return
    _track_msg(update.effective_chat.id, user.id)


# ═══════════════════════════════════════════════════════════════════════════════
# 📋 RO'YXATDAN O'TKAZISH
# ═══════════════════════════════════════════════════════════════════════════════

def register(app: Application) -> None:
    # 🎮 O'yinlar
    app.add_handler(CommandHandler("truth", truth_cmd))
    app.add_handler(CommandHandler("dare", dare_cmd))
    app.add_handler(CommandHandler("wouldyourather", wouldyourather_cmd))
    app.add_handler(CommandHandler("8ball", eightball_cmd))
    app.add_handler(CommandHandler("rps", rps_cmd))

    # 🎲 Fun
    app.add_handler(CommandHandler("roast", roast_cmd))
    app.add_handler(CommandHandler("compliment", compliment_cmd))
    app.add_handler(CommandHandler("horoscope", horoscope_cmd))
    app.add_handler(CommandHandler("lucky", lucky_cmd))
    app.add_handler(CommandHandler("rate", rate_cmd))

    # 💬 Guruh hayoti
    app.add_handler(CommandHandler("quote", quote_cmd))
    app.add_handler(CommandHandler("confession", confession_cmd))
    app.add_handler(CommandHandler("ngl", ngl_cmd))
    app.add_handler(CommandHandler("fakechat", fakechat_cmd))

    # 📊 Statistika
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("who", who_cmd))
    app.add_handler(CommandHandler("activity", activity_cmd))

    # 🌍 Foydali
    app.add_handler(CommandHandler("translate", translate_cmd))
    app.add_handler(CommandHandler("weather", weather_cmd))
    app.add_handler(CommandHandler("calc", calc_cmd))
    app.add_handler(CommandHandler("remind", remind_cmd))

    # 💔 Ship qo'shimchalari
    app.add_handler(CommandHandler("couple", couple_cmd))
    app.add_handler(CommandHandler("divorced", divorced_cmd))
    app.add_handler(CommandHandler("crush", crush_cmd))
    app.add_handler(CommandHandler("soulmate", soulmate_cmd))
    app.add_handler(CommandHandler("exship", exship_cmd))
    app.add_handler(CommandHandler("husband", husband_cmd))
    app.add_handler(CommandHandler("wife", wife_cmd))

    # Callback handlerlar
    app.add_handler(CallbackQueryHandler(wyr_vote_cb, pattern=r"^wyr:"))
    app.add_handler(CallbackQueryHandler(rps_cb, pattern=r"^rps:"))

    # Xabar kuzatuvchi (statistika uchun)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _track_fun_messages),
        group=3
    )
