"""
Misumi AI — shipplus.py  (ship / nikoh tizimiga qo'shimchalar)

🏷  Telegram guruh TEGLARI: nikohlangan / sevgili juftlarga avtomatik teg ("Juft: Aziza")
    (bot guruhda admin bo'lib, "Teglarni boshqarish" huquqi bo'lishi kerak. Teg 16 belgigacha, emojisiz.)
    /shiptag on|off  — (guruh admini) teglarni yoqish/o'chirish
💔 /ajrashish        — nikohni bekor qilish (tasdiqlash tugmasi bilan, teg ham olib tashlanadi)
💞 /juftim           — o'z juftingiz kartasi: necha kun birga, juft ismi, sevgi foizi
🎂 Yubileylar        — 7, 30, 100, 365... kunlik yubileyda bot guruhga tabrik yuboradi
🔒 Qo'sh nikoh yo'q  — nikohdagi odamga yangi taklif yuborib bo'lmaydi

ship.py bilan ulanishi: ship.register() oxirida shipplus.register(app) chaqiriladi.
"""
import asyncio
import html
import logging
import random
import re
import time
from datetime import datetime, time as dtime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

logger = logging.getLogger("misumi-shipplus")

MILESTONES = {7: "1 hafta", 30: "1 oy", 50: "50 kun", 100: "100 kun", 200: "200 kun",
              365: "1 yil", 500: "500 kun", 730: "2 yil", 1000: "1000 kun", 1095: "3 yil"}
_TAG_RIGHT_CACHE: dict[int, tuple[float, bool]] = {}
_HINTED: set[int] = set()
_TAGS_OFF_FILE = "ship_tags_off.json"


def _ship():
    import ship
    return ship


# ═══════════════════════════ yordamchilar ═══════════════════════════

def married_to(chat_id: int, uid: int) -> dict | None:
    for c in _ship()._married_couples.get(chat_id, []):
        if uid in (c.get("id1"), c.get("id2")):
            return c
    return None


def dating_with(chat_id: int, uid: int) -> dict | None:
    for c in _ship()._dating_couples.get(chat_id, []):
        if uid in (c.get("id1"), c.get("id2")):
            return c
    return None


def _days_since(date_s: str) -> int:
    try:
        d = datetime.strptime(date_s, "%Y-%m-%d")
        return max(0, (datetime.now() - d).days)
    except Exception:
        return 0


def days_text(date_s: str) -> str:
    n = _days_since(date_s)
    return "bugun" if n == 0 else f"{n} kun"


def pick_single(chat_id: int, picker):
    """Tasodifiy juftlik tanlaganda nikohdagilarni o'tkazib yuboradi (10 urinish)."""
    for _ in range(10):
        pair = picker(chat_id)
        if not pair:
            return None
        if not married_to(chat_id, pair[0]) and not married_to(chat_id, pair[1]):
            return pair
    return None


def _love_pct(id1: int, id2: int) -> int:
    a, b = sorted((id1, id2))
    return random.Random(f"{a}-{b}").randint(62, 100)      # bir juft uchun doim bir xil


def _tags_off(chat_id: int) -> bool:
    s = _ship()
    from pathlib import Path
    data = s._load_json(Path(s.SHIP_MEMORY_DIR.parent) / _TAGS_OFF_FILE, [])
    return chat_id in data


def _set_tags_off(chat_id: int, off: bool) -> None:
    s = _ship()
    from pathlib import Path
    path = Path(s.SHIP_MEMORY_DIR.parent) / _TAGS_OFF_FILE
    data = set(s._load_json(path, []))
    (data.add if off else data.discard)(chat_id)
    s._save_json(path, sorted(data))


# ═══════════════════════════ TEGLAR ═══════════════════════════

def _tag_text(kind: str, partner_name: str) -> str:
    name = re.sub(r"[^\w ]", "", partner_name or "", flags=re.UNICODE).replace("_", "").strip()
    prefix = "Juft: " if kind == "marry" else "Sevgili: "
    room = 16 - len(prefix)
    return (prefix + name[:room]).strip() if name else ("Nikohda" if kind == "marry" else "Sevgilisi bor")


async def _can_manage_tags(bot, chat_id: int) -> bool:
    cached = _TAG_RIGHT_CACHE.get(chat_id)
    if cached and time.time() - cached[0] < 600:
        return cached[1]
    ok = False
    try:
        me = await bot.get_chat_member(chat_id, bot.id)
        ok = me.status == "creator" or bool(getattr(me, "can_manage_tags", False))
    except TelegramError:
        ok = False
    _TAG_RIGHT_CACHE[chat_id] = (time.time(), ok)
    return ok


async def set_tag(bot, chat_id: int, uid: int, tag: str | None) -> bool:
    try:
        await bot.set_chat_member_tag(chat_id=chat_id, user_id=uid, tag=tag)
        return True
    except TelegramError as e:
        logger.info("teg qo'yilmadi (%s, %s): %s", chat_id, uid, e)
        return False
    except Exception:
        return False


async def apply_tags(context, chat_id: int, c: dict, kind: str) -> int:
    """Ikkala juftga teg qo'yadi. Nechta teg qo'yilganini qaytaradi."""
    if _tags_off(chat_id) or not await _can_manage_tags(context.bot, chat_id):
        return 0
    n = 0
    n += await set_tag(context.bot, chat_id, c["id1"], _tag_text(kind, c["name2"]))
    n += await set_tag(context.bot, chat_id, c["id2"], _tag_text(kind, c["name1"]))
    return n


async def on_couple(context, chat_id: int, id1: int, id2: int, kind: str) -> None:
    """ship.py ceremonialaridan chaqiriladi (nikoh yoki sevgi e'lonidan keyin)."""
    s = _ship()
    try:
        pool = s._married_couples if kind == "marry" else s._dating_couples
        c = next((x for x in reversed(pool.get(chat_id, []))
                  if {x["id1"], x["id2"]} == {id1, id2}), None)
        if not c:
            return
        if kind == "marry":                       # nikohlangan bo'lsa, "sevgili" yozuvi o'chadi
            lst = s._dating_couples.get(chat_id, [])
            keep = [x for x in lst if {x["id1"], x["id2"]} != {id1, id2}]
            if len(keep) != len(lst):
                s._dating_couples[chat_id] = keep
                s._save_dating()
        n = await apply_tags(context, chat_id, c, kind)
        if n == 0 and not _tags_off(chat_id) and chat_id not in _HINTED \
                and not await _can_manage_tags(context.bot, chat_id):
            _HINTED.add(chat_id)
            await context.bot.send_message(
                chat_id,
                "🏷 <i>Maslahat: guruh adminlari menga «Teglarni boshqarish» huquqini bersa, "
                "nikohlanganlarga avtomatik teg qo'yaman (masalan «Juft: Aziza»).</i>",
                parse_mode="HTML",
            )
    except Exception:
        logger.exception("on_couple xato")


async def cmd_shiptag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or chat.type not in ("group", "supergroup"):
        return
    try:
        st = (await context.bot.get_chat_member(chat.id, user.id)).status
    except TelegramError:
        return
    if st not in ("administrator", "creator"):
        await msg.reply_text("faqat guruh adminlari o'zgartira oladi.")
        return
    arg = (context.args[0].lower() if context.args else "")
    if arg in ("off", "ochir", "o'chir"):
        _set_tags_off(chat.id, True)
        await msg.reply_text("🏷 teglar o'chirildi.")
    elif arg in ("on", "yoq", "yoq"):
        _set_tags_off(chat.id, False)
        _TAG_RIGHT_CACHE.pop(chat.id, None)
        ok = await _can_manage_tags(context.bot, chat.id)
        await msg.reply_text("🏷 teglar yoqildi." + ("" if ok else
                             "\n⚠️ Lekin menda «Teglarni boshqarish» huquqi yo'q — admin qilib shu huquqni bering."))
    else:
        state = "o'chiq" if _tags_off(chat.id) else "yoqilgan"
        await msg.reply_text(f"🏷 teglar hozir: {state}\n/shiptag on yoki /shiptag off")


# ═══════════════════════════ /ajrashish ═══════════════════════════

async def cmd_ajrashish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or chat.type not in ("group", "supergroup"):
        if msg:
            await msg.reply_text("❌ Faqat guruhlarda ishlaydi!")
        return
    c = married_to(chat.id, user.id)
    if not c:
        await msg.reply_text("sen hozir nikohda emassan 🤷  (/marry)")
        return
    partner = c["name2"] if user.id == c["id1"] else c["name1"]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💔 Ha, ajrashaman", callback_data=f"dv:y:{user.id}"),
        InlineKeyboardButton("❤️ Yo'q, qolaman", callback_data=f"dv:n:{user.id}"),
    ]])
    await msg.reply_text(
        f"💔 <b>{html.escape(partner)}</b> bilan nikohni bekor qilasanmi?\n"
        f"Birga bo'lgan vaqt: <b>{days_text(c['date'])}</b>",
        reply_markup=kb, parse_mode="HTML",
    )


async def divorce_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = (q.data or "").split(":")
    if len(parts) != 3:
        await q.answer()
        return
    _, ans, uid_s = parts
    if q.from_user.id != int(uid_s):
        await q.answer("bu tugma senga emas", show_alert=True)
        return
    chat_id = q.message.chat_id
    if ans == "n":
        await q.answer("yashasin sevgi!")
        await q.message.edit_text("❤️ Nikoh saqlanib qoldi. Yashasin sevgi!")
        return
    s = _ship()
    c = married_to(chat_id, q.from_user.id)
    if not c:
        await q.answer("nikoh topilmadi", show_alert=True)
        return
    days = days_text(c["date"])
    s._married_couples[chat_id] = [x for x in s._married_couples.get(chat_id, []) if x is not c]
    s._save_married()
    for u in (c["id1"], c["id2"]):
        if _ship()._last_personal_ship.get(chat_id, {}).get(u, {}).get("kind") == "marry":
            s._last_personal_ship[chat_id].pop(u, None)
    s._save_personal()
    if not _tags_off(chat_id) and await _can_manage_tags(context.bot, chat_id):
        await set_tag(context.bot, chat_id, c["id1"], None)
        await set_tag(context.bot, chat_id, c["id2"], None)
    await q.answer("ajrashdingiz")
    await q.message.edit_text(
        f"💔 <b>{html.escape(c['name1'])}</b> va <b>{html.escape(c['name2'])}</b> ajrashdi.\n"
        f"{days} birga bo'ldi. Guruh hamdard 🕊", parse_mode="HTML")


# ═══════════════════════════ /juftim ═══════════════════════════

async def cmd_juftim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or chat.type not in ("group", "supergroup"):
        if msg:
            await msg.reply_text("❌ Faqat guruhlarda ishlaydi!")
        return
    s = _ship()
    c, kind = married_to(chat.id, user.id), "marry"
    if not c:
        c, kind = dating_with(chat.id, user.id), "dating"
    if not c:
        await msg.reply_text("hozircha juftingiz yo'q 🥲\n/marry yoki /dating bilan boshlang.")
        return
    other_id = c["id2"] if user.id == c["id1"] else c["id1"]
    other = c["name2"] if user.id == c["id1"] else c["name1"]
    me = user.first_name or str(user.id)
    pct = _love_pct(user.id, other_id)
    title, icon = ("NIKOH", "💍") if kind == "marry" else ("SEVGI", "💞")
    nxt = ""
    n = _days_since(c["date"])
    upcoming = [d for d in sorted(MILESTONES) if d > n]
    if upcoming:
        nxt = f"\n🎯 Keyingi yubiley: <b>{MILESTONES[upcoming[0]]}</b> ({upcoming[0] - n} kundan keyin)"
    await msg.reply_text(
        f"{icon} <b>{title} KARTASI</b> {icon}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👫 <b>{html.escape(me)}</b> + <b>{html.escape(other)}</b>\n"
        f"🏷 Juft ismi: <b>{html.escape(s._ship_name(me, other))}</b>\n"
        f"📅 Boshlangan: {c['date']}\n"
        f"⏳ Birga: <b>{days_text(c['date'])}</b>\n"
        f"💗 Sevgi: <b>{pct}%</b>  {s._couple_bar(pct)}"
        f"{nxt}", parse_mode="HTML")


# ═══════════════════════════ yubileylar + saqlash ═══════════════════════════

async def anniversary_job(context: ContextTypes.DEFAULT_TYPE):
    s = _ship()
    for chat_id, couples in list(s._married_couples.items()):
        for c in couples:
            n = _days_since(c.get("date", ""))
            if n in MILESTONES:
                try:
                    await context.bot.send_message(
                        chat_id,
                        f"🎂 <b>{html.escape(c['name1'])}</b> va <b>{html.escape(c['name2'])}</b> nikohiga "
                        f"<b>{MILESTONES[n]}</b> to'ldi! 🥳💍\nTabriklaymiz, guruh!",
                        parse_mode="HTML")
                except TelegramError:
                    pass
                await asyncio.sleep(0.5)


async def flush_job(context: ContextTypes.DEFAULT_TYPE):
    """A'zolar ro'yxati bazaga throttle bilan yoziladi (har xabarda emas)."""
    try:
        _ship().flush_seen_members()
    except Exception:
        logger.exception("flush xato")


def register(app: Application) -> None:
    app.add_handler(CommandHandler("ajrashish", cmd_ajrashish))
    app.add_handler(CommandHandler(["juftim", "mypair"], cmd_juftim))
    app.add_handler(CommandHandler("shiptag", cmd_shiptag))
    app.add_handler(CallbackQueryHandler(divorce_callback, pattern=r"^dv:"))
    jq = getattr(app, "job_queue", None)
    if jq:
        jq.run_daily(anniversary_job, time=dtime(hour=4, minute=0, tzinfo=timezone.utc), name="ship_anniv")  # 09:00 Toshkent
        jq.run_repeating(flush_job, interval=45, first=45, name="ship_flush")
    else:
        logger.warning("job_queue yo'q — yubileylar o'chiq")
