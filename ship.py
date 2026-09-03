"""
Misumi AI — /ship komandasi (juftlik tanlash).

Guruh a'zolaridan tasodifiy erkak va qiz juftlikni tanlab,
ularni «sevishgan juft» sifatida e'lon qiladi. Love rate va
status shuningdek AI tomonidan yoziladi.

Bot guruhda so'nggi xabar yuborgan foydalanuvchilarni eslab
qoladi (xotira: _seen_members). Agar a'zolar yetarli bo'lmasa,
bot get_chat_members() orqali olishga harakat qiladi.

Hamkor: bot.py dan register(app) chaqiriladi.
"""
import asyncio
import random
import time
from collections import defaultdict

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import ai_core

# ── xotira ──────────────────────────────────────────────────────────────────
# {chat_id: {user_id: {"name": str, "last": float}}}
_seen_members: dict[int, dict[int, dict]] = defaultdict(dict)

# /ship cooldown: guruh boshiga (chat_id → unix_ts)
_last_ship: dict[int, float] = {}
SHIP_COOLDOWN = 16 * 60  # 16 daqiqa


# ── a'zoni qayd etish ────────────────────────────────────────────────────────

def record_member(chat_id: int, user_id: int, first_name: str) -> None:
    """Har qanday xabar kelganda a'zoni eslab qo'yish uchun chaqiriladi."""
    _seen_members[chat_id][user_id] = {
        "name": first_name or str(user_id),
        "last": time.time(),
    }


# ── AI kontent generatsiyasi ─────────────────────────────────────────────────

def _generate_ship_caption(name1: str, name2: str, love_rate: int) -> str:
    """AI yordamida qisqa, romantik juftlik iborasi yaratadi."""
    instruction = (
        "You are Misumi AI, a fun and warm Telegram group bot. "
        "Write ONE short romantic caption (1–2 sentences, Uzbek informal) "
        "announcing that two people have been matched as a couple. "
        "Be sweet but a little playful — like a friend teasing them. "
        "Do NOT include the love rate number or the names in the caption "
        "(they are shown separately). Output ONLY the caption, no quotes."
    )
    prompt = (
        f"Juft: {name1} + {name2}. Sevgi darajasi: {love_rate}%."
    )
    fallback_options = [
        "Tabiat o'zi tanladi — bu juftlikka hech kim qarshi tura olmaydi! 💘",
        "Ba'zi narsalar tasodif emas, bu ham shulardan! 🌹",
        "Ko'zlar gaplashaveradi, ular esa allaqachon gaplashib bo'lgan! ✨",
        "Yuraklar bir-birini tanladi — bu endi sir emas! 💕",
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
                continue
    except Exception:
        pass
    return random.choice(fallback_options)


def _generate_status_line(love_rate: int) -> str:
    """Love rate asosida qisqa status matni."""
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


# ── asosiy komanda ───────────────────────────────────────────────────────────

async def ship_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.message

    # Faqat guruhlarda ishlaydi
    if chat.type not in ("group", "supergroup"):
        await message.reply_text("Bu komanda faqat guruhlarda ishlaydi! 👥")
        return

    chat_id = chat.id
    now = time.time()

    # Cooldown tekshirish
    last = _last_ship.get(chat_id, 0)
    remaining = SHIP_COOLDOWN - (now - last)
    if remaining > 0:
        mins = int(remaining // 60)
        secs = int(remaining % 60)
        await message.reply_text(
            f"⏳ Keyingi juftlikni ko'rish uchun {mins}:{secs:02d} kuting!"
        )
        return

    # Guruh a'zolarini yig'ish
    members = dict(_seen_members.get(chat_id, {}))

    # Agar yetarli a'zo bo'lmasa, admin API dan olishga urinib ko'ramiz
    if len(members) < 2:
        try:
            admins = await context.bot.get_chat_administrators(chat_id)
            for admin in admins:
                u = admin.user
                if not u.is_bot:
                    members[u.id] = {
                        "name": u.first_name or str(u.id),
                        "last": 0,
                    }
        except Exception:
            pass

    if len(members) < 2:
        await message.reply_text(
            "Juft tanlash uchun guruhda kamida 2 ta a'zo kerak! "
            "Avval bir-biringiz xabar yuboring 💬"
        )
        return

    # Tasodifiy 2 ta a'zo tanlash
    ids = list(members.keys())
    p1_id, p2_id = random.sample(ids, 2)
    name1 = members[p1_id]["name"]
    name2 = members[p2_id]["name"]

    love_rate = random.randint(1, 99)
    status_text = _generate_status_line(love_rate)

    # Progress bar (10 ta belgi)
    filled = round(love_rate / 10)
    bar = "█" * filled + "░" * (10 - filled)

    # AI caption
    caption = _generate_ship_caption(name1, name2, love_rate)

    # Ship nomi (ikki ismning birinchi bo'g'inlaridan)
    half1 = name1[:max(1, len(name1) // 2)]
    half2 = name2[len(name2) // 2:]
    ship_name = (half1 + half2).title()

    # Xabar yig'ish
    text = (
        f"💝 MATCHMAKING SUCCESSFUL 💝\n\n"
        f"❤️ {name1} + {name2}  »  {ship_name}\n"
        f"💯 LOVE RATE: {love_rate}%\n"
        f"📊 STATUS: {bar}\n\n"
        f"{caption}\n\n"
        f"📌 {status_text}"
    )

    _last_ship[chat_id] = now
    await message.reply_text(text)


# ── passiv yig'uvchi ─────────────────────────────────────────────────────────

async def _track_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Har qanday guruh xabarida a'zoni qayd etadi (passiv, jim)."""
    msg = update.message
    if not msg:
        return
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        return
    user = update.effective_user
    if not user or user.is_bot:
        return
    record_member(chat.id, user.id, user.first_name or "")


# ── ro'yxatdan o'tkazish ─────────────────────────────────────────────────────

def register(app: Application) -> None:
    """bot.py ning main() funksiyasidan bir marta chaqiriladi."""
    app.add_handler(CommandHandler("ship", ship_cmd))
    # Barcha matn xabarlari orqali a'zolarni yig'ib boradi (group=2)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _track_member),
        group=2,
    )
