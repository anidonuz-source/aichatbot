"""
Misumi AI — /ship komandasi (juftlik tanlash).

Qoidalar:
  • Guruh cooldown: 1 daqiqa
  • Foydalanuvchi limiti: kuniga max 10 marta
  • Limit tugaganda: 1 soat kutish
  • Tanlangan a'zolar @mention bilan tag qilinadi
  • Romantik anime juft rasmi bilan yuboriladi
"""

import random
import time
from collections import defaultdict

from telegram import Update, InputMediaPhoto
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import ai_core

# ── xotira ───────────────────────────────────────────────────────────────────
# {chat_id: {user_id: {"name": str, "username": str|None, "last": float}}}
_seen_members: dict[int, dict[int, dict]] = defaultdict(dict)

_last_ship_group: dict[int, float] = {}
GROUP_COOLDOWN = 60  # 1 daqiqa

_user_ship_times: dict[tuple, list] = defaultdict(list)
USER_DAILY_LIMIT = 10
USER_LIMIT_COOLDOWN = 3600  # 1 soat

# Anime juft rasmlari (public Telegram file_id yoki URL)
COUPLE_IMAGES = [
    "https://i.imgur.com/Q1mIuge.jpeg",
    "https://i.imgur.com/7oXFgT0.jpeg",
    "https://i.imgur.com/tVozFaT.jpeg",
    "https://i.imgur.com/nX4DJLH.jpeg",
    "https://i.imgur.com/pYvFqsk.jpeg",
]


# ── yordamchi funksiyalar ─────────────────────────────────────────────────────

def record_member(chat_id: int, user_id: int, first_name: str, username: str | None = None) -> None:
    _seen_members[chat_id][user_id] = {
        "name": first_name or str(user_id),
        "username": username,
        "last": time.time(),
    }


def _mention(user_id: int, name: str, username: str | None) -> str:
    """Telegram mention: username bo'lsa @username, bo'lmasa inline mention."""
    if username:
        return f"@{username}"
    # username yo'q bo'lsa — tg://user mention (HTML parse_mode bilan)
    return f'<a href="tg://user?id={user_id}">{name}</a>'


def _check_user_limit(chat_id: int, user_id: int) -> tuple[bool, float]:
    key = (chat_id, user_id)
    now = time.time()
    day_ago = now - 86400

    _user_ship_times[key] = [t for t in _user_ship_times[key] if t > day_ago]
    times = _user_ship_times[key]

    if len(times) < USER_DAILY_LIMIT:
        return True, 0.0

    oldest = min(times)
    wait_until = oldest + USER_LIMIT_COOLDOWN
    remaining = wait_until - now
    if remaining <= 0:
        _user_ship_times[key] = times[1:]
        return True, 0.0
    return False, remaining


def _generate_ship_caption(name1: str, name2: str, love_rate: int) -> str:
    instruction = (
        "You are Misumi AI, a fun and warm Telegram group bot. "
        "Write ONE short romantic caption (1–2 sentences, Uzbek informal) "
        "announcing that two people have been matched as a couple. "
        "Be sweet but a little playful — like a friend teasing them. "
        "Do NOT include the love rate number or the names in the caption. "
        "Output ONLY the caption, no quotes."
    )
    prompt = f"Juft: {name1} + {name2}. Sevgi darajasi: {love_rate}%."
    fallbacks = [
        "Ba'zi narsalar tasodif emas, bu ham shulardan! 🌹",
        "Tabiat o'zi tanladi — bu juftlikka hech kim qarshi tura olmaydi! 💘",
        "Ko'zlar gaplashaveradi, ular esa allaqachon gaplashib bo'lgan! ✨",
        "Yuraklar bir-birini tanladi — bu endi sir emas! 💕",
        "Kimdir buni ko'rib ichida kulayapti… biz ham kulyapmiz! 😄",
    ]
    try:
        for call in (ai_core._call_cerebras, ai_core._call_gemini, ai_core._call_groq):
            try:
                text = call(
                    ai_core.GENERAL_CHAT_PERSONA + "\n\n" + instruction,
                    [],
                    prompt,
                ).strip().strip('"')
                if text:
                    return text
            except Exception as e:
                print(f"[ship:ai:{call.__name__}] {e}")
    except Exception:
        pass
    return random.choice(fallbacks)


def _status_line(love_rate: int) -> str:
    if love_rate >= 80:
        return "💞 Hayot sheriklar!"
    elif love_rate >= 60:
        return "🌸 Juda mos juft"
    elif love_rate >= 40:
        return "🌱 Rivojlanmoqda..."
    elif love_rate >= 20:
        return "🤔 Imkoniyat bor"
    else:
        return "😅 Boshlash kerak..."


def _fmt_time(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s} soniya"
    m, sec = divmod(s, 60)
    if sec == 0:
        return f"{m} daqiqa"
    return f"{m} daqiqa {sec} soniya"


# ── asosiy komanda ────────────────────────────────────────────────────────────

async def ship_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.message
    user = update.effective_user

    if chat.type not in ("group", "supergroup"):
        await message.reply_text("Bu komanda faqat guruhlarda ishlaydi! 👥")
        return

    chat_id = chat.id
    user_id = user.id
    now = time.time()

    # 1. Guruh cooldown
    last_group = _last_ship_group.get(chat_id, 0)
    group_remaining = GROUP_COOLDOWN - (now - last_group)
    if group_remaining > 0:
        await message.reply_text(
            f"⏳ Guruh limiti! Keyingi juftlik uchun {_fmt_time(group_remaining)} kuting."
        )
        return

    # 2. Foydalanuvchi kunlik limit
    allowed, user_remaining = _check_user_limit(chat_id, user_id)
    if not allowed:
        await message.reply_text(
            f"🚫 Siz bugun {USER_DAILY_LIMIT} marta /ship ishlatdingiz!\n"
            f"⏳ Qayta ishlatish uchun {_fmt_time(user_remaining)} kuting."
        )
        return

    # 3. A'zolarni yig'ish
    members = dict(_seen_members.get(chat_id, {}))
    if len(members) < 2:
        try:
            admins = await context.bot.get_chat_administrators(chat_id)
            for admin in admins:
                u = admin.user
                if not u.is_bot:
                    members[u.id] = {
                        "name": u.first_name or str(u.id),
                        "username": u.username,
                        "last": 0,
                    }
        except Exception:
            pass

    if len(members) < 2:
        await message.reply_text(
            "Juft tanlash uchun guruhda kamida 2 ta a'zo kerak!\n"
            "Avval bir-biringiz xabar yuboring 💬"
        )
        return

    # 4. Tasodifiy 2 ta a'zo tanlash
    ids = list(members.keys())
    p1_id, p2_id = random.sample(ids, 2)
    m1 = members[p1_id]
    m2 = members[p2_id]
    name1 = m1["name"]
    name2 = m2["name"]

    # Mention (tag)
    tag1 = _mention(p1_id, name1, m1.get("username"))
    tag2 = _mention(p2_id, name2, m2.get("username"))

    love_rate = random.randint(1, 99)
    bar_filled = round(love_rate / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)

    half1 = name1[: max(1, len(name1) // 2)]
    half2 = name2[len(name2) // 2:]
    ship_name = (half1 + half2).title()

    caption = _generate_ship_caption(name1, name2, love_rate)
    status = _status_line(love_rate)

    _user_ship_times[(chat_id, user_id)].append(now)
    _last_ship_group[chat_id] = now

    used = len(_user_ship_times[(chat_id, user_id)])
    left = USER_DAILY_LIMIT - used

    # Caption (HTML parse_mode uchun)
    text = (
        f"💝 MATCHMAKING SUCCESSFUL 💝\n\n"
        f"❤️ {tag1} + {tag2}  »  {ship_name}\n"
        f"💯 LOVE RATE: {love_rate}%\n"
        f"📊 STATUS: {bar}\n\n"
        f"{caption}\n\n"
        f"📌 {status}\n"
        f"─────────────────\n"
        f"🎟 Bugun {left} ta /ship qoldi"
    )

    # Rasm bilan yuborish
    image_url = random.choice(COUPLE_IMAGES)
    try:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=image_url,
            caption=text,
            parse_mode="HTML",
        )
    except Exception as e:
        print(f"[ship:photo] {e} — rasimsiz yuboriladi")
        await message.reply_text(text, parse_mode="HTML")


# ── passiv a'zo yig'uvchi ─────────────────────────────────────────────────────

async def _track_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg:
        return
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        return
    u = update.effective_user
    if not u or u.is_bot:
        return
    record_member(chat.id, u.id, u.first_name or "", u.username)


# ── ro'yxatdan o'tkazish ──────────────────────────────────────────────────────

def register(app: Application) -> None:
    app.add_handler(CommandHandler("ship", ship_cmd))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _track_member),
        group=2,
    )
