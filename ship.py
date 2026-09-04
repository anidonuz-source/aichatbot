"""
Misumi AI — /ship komandasi (juftlik tanlash) — v3 (mukammal)

Yangiliklar v3:
  • Odamday suhbat uslubi — har safar boshqacha, jonli muloqot
  • So'z yodlash — guruh a'zolari haqida faktlar yodlanadi (ism, kasblar, xarakter)
  • Kontekstga mos gap — yodlangan ma'lumotdan foydalanib shaxsiy izoh
  • /ship @user1 @user2 — o'zingiz tanlagan 2 kishi
  • /shipleader — haftalik TOP 5 juftlar
  • /shipfact — guruh a'zosi haqida yodlangan faktni ko'rsatish
  • Yulduz burji mos kelish balli
  • Sevgi animatsiyasi (loading dots)
  • Ko'proq anime rasmlar
  • Chiroyliroq xabar dizayni

Xotira tizimi:
  • Foydalanuvchi o'z haqida biror narsa aytsa — ship yodlab qo'yadi
  • Keyingi ship da o'sha ma'lumot ishlatiladi (ism, kasb, xarakter, sevimli narsa)
  • /shipfact @user — u haqida nima bilinishini ko'rsatadi

Qoidalar:
  • Guruh cooldown: 1 daqiqa
  • Foydalanuvchi limiti: kuniga max 10 marta
  • Limit tugaganda: 1 soat kutish
"""

import json
import os
import random
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from threading import Lock

import requests
from telegram import Update, InputMediaPhoto
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import ai_core

# ── xotira ───────────────────────────────────────────────────────────────────
_seen_members: dict[int, dict[int, dict]] = defaultdict(dict)
_last_ship_group: dict[int, float] = {}
GROUP_COOLDOWN = 60

_user_ship_times: dict[tuple, list] = defaultdict(list)
USER_DAILY_LIMIT = 10
USER_LIMIT_COOLDOWN = 3600

# TOP juftlar: {chat_id: {(id1,id2): {"count": int, "names": (n1,n2), "last": float}}}
_couple_leaderboard: dict[int, dict[tuple, dict]] = defaultdict(dict)

# ── Ship xotirasi (disk, persistent) ─────────────────────────────────────────
SHIP_MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", "memory")) / "ship_members"
SHIP_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
_ship_mem_lock = Lock()

SHIP_KNOWN_FACTS = [
    "ism", "kasb", "yosh", "shahar", "xarakter", "sevimli_narsa",
    "qiziqish", "kayfiyat", "qo'shiqchi", "film", "sport", "orzular"
]


def _member_mem_path(chat_id: int, user_id: int) -> Path:
    return SHIP_MEMORY_DIR / f"{chat_id}_{user_id}.json"


def load_member_facts(chat_id: int, user_id: int) -> dict:
    path = _member_mem_path(chat_id, user_id)
    if not path.exists():
        return {}
    with _ship_mem_lock:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}


def save_member_facts(chat_id: int, user_id: int, facts: dict) -> None:
    if not facts:
        return
    path = _member_mem_path(chat_id, user_id)
    existing = load_member_facts(chat_id, user_id)
    existing.update(facts)
    with _ship_mem_lock:
        path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")


def format_member_facts(facts: dict, name: str) -> str:
    """Yodlangan faktlarni AI uchun tayyorlaydi."""
    if not facts:
        return ""
    lines = [f"{name} haqida ma'lumot:"]
    for k, v in facts.items():
        lines.append(f"  - {k}: {v}")
    return "\n".join(lines)


def _extract_facts_from_text(text: str) -> dict:
    """Foydalanuvchi gapidan faktlarni ajratib olish uchun AI dan foydalanadi."""
    instruction = (
        "Sen Telegram guruh botissan. "
        "Quyidagi xabardan foydalanuvchi haqida uzoq muddatli faktlarni ajrat: "
        "ism, kasb, yosh, shahar, xarakter, sevimli_narsa, qiziqish va shunga o'xshashlar. "
        "FAQAT JSON qaytargin (bo'sh obyekt {} bo'lishi ham mumkin), boshqa narsa yozma. "
        "Misol: {\"kasb\": \"dasturchi\", \"shahar\": \"Toshkent\"} "
        "Agar hech narsa topilmasa: {}"
    )
    fallbacks = {}
    try:
        for call in (ai_core._call_cerebras, ai_core._call_gemini, ai_core._call_groq):
            try:
                raw = call(instruction, [], text).strip()
                # JSON ni tozalab olish
                if "```" in raw:
                    raw = raw.split("```")[1].replace("json", "").strip()
                data = json.loads(raw)
                if isinstance(data, dict):
                    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
            except Exception as e:
                print(f"[ship:facts:{call.__name__}] {e}")
    except Exception:
        pass
    return fallbacks


# ── rasmlar ───────────────────────────────────────────────────────────────────
COUPLE_IMAGES = [
    "https://nekos.best/api/v2/kiss/0001.png",
    "https://nekos.best/api/v2/kiss/0002.png",
    "https://nekos.best/api/v2/kiss/0003.png",
    "https://nekos.best/api/v2/kiss/0004.png",
    "https://nekos.best/api/v2/kiss/0005.png",
    "https://nekos.best/api/v2/hug/0001.png",
    "https://nekos.best/api/v2/hug/0002.png",
    "https://nekos.best/api/v2/hug/0003.png",
    "https://nekos.best/api/v2/hug/0004.png",
    "https://nekos.best/api/v2/hug/0005.png",
    "https://nekos.best/api/v2/cuddle/0001.png",
    "https://nekos.best/api/v2/cuddle/0002.png",
    "https://nekos.best/api/v2/cuddle/0003.png",
    "https://nekos.best/api/v2/cuddle/0004.png",
    "https://nekos.best/api/v2/cuddle/0005.png",
]


def _get_couple_image() -> str:
    categories = ["kiss", "hug", "cuddle"]
    cat = random.choice(categories)
    try:
        resp = requests.get(f"https://nekos.best/api/v2/{cat}", timeout=5)
        if resp.ok:
            data = resp.json()
            return data["results"][0]["url"]
    except Exception:
        pass
    return random.choice(COUPLE_IMAGES)


# ── yulduz burjlari ────────────────────────────────────────────────────────────
ZODIAC_SIGNS = [
    "♈ Qo'y", "♉ Ho'kiz", "♊ Egizaklar", "♋ Qisqichbaqa",
    "♌ Sher", "♍ Boshoq", "♎ Tarozi", "♏ Chayon",
    "♐ Sagitarius", "♑ Tog' echkisi", "♒ Qovg'a", "♓ Baliq"
]

ZODIAC_COMPAT = {
    (0, 4): 95, (0, 6): 88, (0, 8): 90,
    (1, 5): 95, (1, 9): 92, (1, 3): 85,
    (2, 6): 90, (2, 10): 88, (2, 4): 82,
    (3, 7): 95, (3, 11): 90, (3, 5): 85,
    (4, 8): 92, (4, 6): 88,
    (5, 9): 95, (5, 7): 88,
    (6, 10): 90, (6, 8): 85,
    (7, 11): 92, (7, 9): 85,
    (8, 10): 88,
    (9, 11): 85,
}


def _zodiac_compat(z1: int, z2: int) -> int:
    key = (min(z1, z2), max(z1, z2))
    return ZODIAC_COMPAT.get(key, random.randint(45, 75))


# ── yordamchi funksiyalar ─────────────────────────────────────────────────────

def record_member(chat_id: int, user_id: int, first_name: str, username: str | None = None) -> None:
    _seen_members[chat_id][user_id] = {
        "name": first_name or str(user_id),
        "username": username,
        "last": time.time(),
        "zodiac": _seen_members[chat_id].get(user_id, {}).get("zodiac", random.randint(0, 11)),
    }


def _mention(user_id: int, name: str, username: str | None) -> str:
    if username:
        return f"@{username}"
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


def _generate_ship_caption(
    name1: str, name2: str, love_rate: int,
    facts1: dict | None = None, facts2: dict | None = None
) -> str:
    """
    Odamday, jonli va shaxsiy ship izohi — yodlangan faktlardan foydalanadi.
    """
    instruction = (
        "Sen Misumi AI — Telegram guruhidagi do'stona, hazilkash botsan. "
        "Ikki kishini ship qiluvchi BITTA qisqa gap yoz (1-2 jumla, o'zbek tili, norasmiy). "
        "Agar ular haqida ma'lumot berilsa — o'sha ma'lumotni aqlli ishlatgin "
        "(masalan kasbiga, shahariga, xarakteriga ishora qil). "
        "Goh hazil, goh romantik, goh do'stona — har safar boshqacha uslubda yoz. "
        "Ular ismini yoki sevgi foizini takrorlama. "
        "FAQAT izoh matnini yoz — hech qanday qo'shimcha narsa yo'q."
    )

    context_parts = [f"Juft: {name1} + {name2}. Sevgi: {love_rate}%."]
    if facts1:
        context_parts.append(format_member_facts(facts1, name1))
    if facts2:
        context_parts.append(format_member_facts(facts2, name2))
    prompt = "\n".join(context_parts)

    fallbacks = [
        "Ba'zi narsalar tasodif emas, bu ham shulardan! 🌹",
        "Tabiat o'zi tanladi — bu juftlikka hech kim qarshi tura olmaydi! 💘",
        "Ko'zlar gaplashaveradi, ular esa allaqachon gaplashib bo'lgan! ✨",
        "Yuraklar bir-birini tanladi — bu endi sir emas! 💕",
        "Kimdir buni ko'rib ichida kulayapti… biz ham kulyapmiz! 😄",
        "Ular uchrashadimi, uchrashmaydi — ammo qismat boshqacha o'ylaydi! 🔮",
        "Bu guruhda eng chiroyli juft! Hammaga baxt tilaymiz 🎊",
        "Yulduzlar ham bugun shu juftni tasdiqladi! ⭐",
        "Ikkalasini ko'rganda bilib bo'lardi bu bo'lishini… 🤭",
        "Shu guruhda ko'pchilik kutgan juft nihoyat rasman bo'ldi! 🎉",
    ]

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
                print(f"[ship:caption:{call.__name__}] {e}")
    except Exception:
        pass
    return random.choice(fallbacks)


def _status_line(love_rate: int) -> str:
    if love_rate >= 90:
        return "💞 Taqdir juftligi! Nikohga tayyorlanish kerak!"
    elif love_rate >= 75:
        return "💖 Juda kuchli mos kelish!"
    elif love_rate >= 60:
        return "🌸 Yaxshi juft, kelajak porloq!"
    elif love_rate >= 45:
        return "🌱 Imkoniyat bor, ishlash kerak!"
    elif love_rate >= 25:
        return "🤔 Qiyin, lekin har narsaga umid bor..."
    else:
        return "😅 Do'stlikdan boshlanadi hamma narsa!"


def _love_bar(love_rate: int) -> str:
    filled = round(love_rate / 10)
    hearts = "❤️" * filled + "🖤" * (10 - filled)
    return hearts


def _fmt_time(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s} soniya"
    m, sec = divmod(s, 60)
    if sec == 0:
        return f"{m} daqiqa"
    return f"{m} daqiqa {sec} soniya"


def _ship_name(name1: str, name2: str) -> str:
    half1 = name1[: max(1, len(name1) // 2)]
    half2 = name2[len(name2) // 2:]
    return (half1 + half2).title()


def _update_leaderboard(chat_id: int, id1: int, id2: int, name1: str, name2: str) -> None:
    key = (min(id1, id2), max(id1, id2))
    if key not in _couple_leaderboard[chat_id]:
        _couple_leaderboard[chat_id][key] = {"count": 0, "names": (name1, name2), "last": 0}
    _couple_leaderboard[chat_id][key]["count"] += 1
    _couple_leaderboard[chat_id][key]["last"] = time.time()


def _build_message(
    tag1, tag2, name1, name2, love_rate, caption,
    zodiac1, zodiac2, used,
    facts1: dict | None = None, facts2: dict | None = None
) -> str:
    bar = _love_bar(love_rate)
    status = _status_line(love_rate)
    ship_nm = _ship_name(name1, name2)
    z1_name = ZODIAC_SIGNS[zodiac1]
    z2_name = ZODIAC_SIGNS[zodiac2]
    zcompat = _zodiac_compat(zodiac1, zodiac2)
    left = USER_DAILY_LIMIT - used

    # Sevgi rangi
    if love_rate >= 80:
        heart = "💗"
    elif love_rate >= 50:
        heart = "💛"
    else:
        heart = "🩶"

    # Yodlangan faktlar qismi (agar bo'lsa)
    facts_section = ""
    fact_lines = []
    if facts1:
        top = list(facts1.items())[:2]
        for k, v in top:
            fact_lines.append(f"   {name1}: {k} — {v}")
    if facts2:
        top = list(facts2.items())[:2]
        for k, v in top:
            fact_lines.append(f"   {name2}: {k} — {v}")
    if fact_lines:
        facts_section = (
            "\n🧠 <b>Ular haqida bilganimiz:</b>\n"
            + "\n".join(fact_lines)
            + "\n"
        )

    return (
        f"╔══════════════════════╗\n"
        f"║  💘  MISUMI MATCHMAKER  💘  ║\n"
        f"╚══════════════════════╝\n\n"
        f"{heart} {tag1}\n"
        f"       +\n"
        f"{heart} {tag2}\n\n"
        f"🏷 Juft ismi: <b>{ship_nm}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💯 Sevgi darajasi: <b>{love_rate}%</b>\n"
        f"{bar}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔮 Burj mos kelishi:\n"
        f"   {z1_name}  🤝  {z2_name}\n"
        f"   ⭐ Mos kelish: <b>{zcompat}%</b>\n"
        f"{facts_section}\n"
        f"💬 <i>{caption}</i>\n\n"
        f"📌 {status}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎟 Bugun <b>{left}</b> ta /ship qoldi"
    )


# ── /ship komandasi ───────────────────────────────────────────────────────────

async def ship_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.message
    user = update.effective_user

    if chat.type not in ("group", "supergroup"):
        await message.reply_text("❌ Bu komanda faqat guruhlarda ishlaydi! 👥")
        return

    chat_id = chat.id
    user_id = user.id
    now = time.time()

    # 1. Guruh cooldown
    last_group = _last_ship_group.get(chat_id, 0)
    group_remaining = GROUP_COOLDOWN - (now - last_group)
    if group_remaining > 0:
        await message.reply_text(
            f"⏳ <b>Guruh limiti!</b>\n"
            f"Keyingi juftlik uchun <b>{_fmt_time(group_remaining)}</b> kuting.",
            parse_mode="HTML"
        )
        return

    # 2. Foydalanuvchi kunlik limit
    allowed, user_remaining = _check_user_limit(chat_id, user_id)
    if not allowed:
        await message.reply_text(
            f"🚫 Siz bugun <b>{USER_DAILY_LIMIT}</b> marta /ship ishlatdingiz!\n"
            f"⏳ Qayta ishlatish uchun <b>{_fmt_time(user_remaining)}</b> kuting.",
            parse_mode="HTML"
        )
        return

    # 3. Loading animatsiyasi
    loading_msgs = [
        "💘 Juft qidirilmoqda...",
        "💘 Juft qidirilmoqda... ❤️",
        "💘 Yulduzlar tekshirilmoqda... 🌟",
        "💘 Qismat hal qilmoqda... 🔮",
    ]
    loading_msg = await message.reply_text(loading_msgs[0])
    await loading_msg.edit_text(random.choice(loading_msgs[1:]))

    # 4. Mention orqali /ship @user1 @user2
    p1_id = p2_id = None
    m1 = m2 = None

    if message.entities and context.args:
        mentioned = [
            e for e in message.entities
            if e.type == "mention" or e.type == "text_mention"
        ]
        if len(mentioned) >= 2:
            e1, e2 = mentioned[0], mentioned[1]
            if e1.type == "text_mention" and e2.type == "text_mention":
                p1_id = e1.user.id
                p2_id = e2.user.id
                m1 = {
                    "name": e1.user.first_name or str(p1_id),
                    "username": e1.user.username,
                    "zodiac": _seen_members[chat_id].get(p1_id, {}).get("zodiac", random.randint(0, 11))
                }
                m2 = {
                    "name": e2.user.first_name or str(p2_id),
                    "username": e2.user.username,
                    "zodiac": _seen_members[chat_id].get(p2_id, {}).get("zodiac", random.randint(0, 11))
                }

    # 5. Agar mention yo'q — tasodifiy tanlash
    if p1_id is None:
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
                            "zodiac": random.randint(0, 11),
                        }
            except Exception:
                pass

        if len(members) < 2:
            await loading_msg.edit_text(
                "❌ Juft tanlash uchun guruhda kamida 2 ta a'zo kerak!\n"
                "Avval bir-biringiz xabar yuboring 💬"
            )
            return

        ids = list(members.keys())
        p1_id, p2_id = random.sample(ids, 2)
        m1 = members[p1_id]
        m2 = members[p2_id]

    name1 = m1["name"]
    name2 = m2["name"]
    zodiac1 = m1.get("zodiac", random.randint(0, 11))
    zodiac2 = m2.get("zodiac", random.randint(0, 11))

    tag1 = _mention(p1_id, name1, m1.get("username"))
    tag2 = _mention(p2_id, name2, m2.get("username"))

    # 6. Yodlangan faktlarni yuklash
    facts1 = load_member_facts(chat_id, p1_id)
    facts2 = load_member_facts(chat_id, p2_id)

    love_rate = random.randint(1, 99)
    caption = _generate_ship_caption(name1, name2, love_rate, facts1 or None, facts2 or None)

    _user_ship_times[(chat_id, user_id)].append(now)
    _last_ship_group[chat_id] = now
    _update_leaderboard(chat_id, p1_id, p2_id, name1, name2)

    used = len(_user_ship_times[(chat_id, user_id)])
    text = _build_message(
        tag1, tag2, name1, name2, love_rate, caption,
        zodiac1, zodiac2, used,
        facts1 or None, facts2 or None
    )

    image_url = _get_couple_image()
    try:
        await loading_msg.delete()
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=image_url,
            caption=text,
            parse_mode="HTML",
        )
    except Exception as e:
        print(f"[ship:photo] {e}")
        await loading_msg.edit_text(text, parse_mode="HTML")


# ── /shipleader komandasi ─────────────────────────────────────────────────────

async def shipleader_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.message

    if chat.type not in ("group", "supergroup"):
        await message.reply_text("❌ Bu komanda faqat guruhlarda ishlaydi!")
        return

    chat_id = chat.id
    board = _couple_leaderboard.get(chat_id, {})

    if not board:
        await message.reply_text(
            "📊 Hali hech qanday juft yo'q!\n"
            "/ship bilan boshlang 💘",
            parse_mode="HTML"
        )
        return

    sorted_couples = sorted(board.items(), key=lambda x: x[1]["count"], reverse=True)[:5]
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    lines = ["🏆 <b>GURUH TOP JUFTLARI</b> 🏆\n━━━━━━━━━━━━━━━━━━━━━━━\n"]

    for i, (key, data) in enumerate(sorted_couples):
        n1, n2 = data["names"]
        count = data["count"]
        sname = _ship_name(n1, n2)
        lines.append(f"{medals[i]} <b>{n1}</b> + <b>{n2}</b>  →  {sname}")
        lines.append(f"   💘 {count} marta ship qilindi\n")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("💡 /ship bilan yangi juft yarating!")

    await message.reply_text("\n".join(lines), parse_mode="HTML")


# ── /shipfact komandasi ───────────────────────────────────────────────────────

async def shipfact_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /shipfact — o'zingiz haqingizda nima yodlanganini ko'rsatadi
    /shipfact @user — boshqa a'zo haqida (agar ma'lumot bo'lsa)
    """
    chat = update.effective_chat
    message = update.message
    user = update.effective_user

    if chat.type not in ("group", "supergroup"):
        await message.reply_text("❌ Bu komanda faqat guruhlarda ishlaydi!")
        return

    chat_id = chat.id
    target_id = user.id
    target_name = user.first_name or "Siz"

    # Mention orqali boshqani ko'rish
    if message.entities:
        for e in message.entities:
            if e.type == "text_mention" and e.user:
                target_id = e.user.id
                target_name = e.user.first_name or str(target_id)
                break

    facts = load_member_facts(chat_id, target_id)

    if not facts:
        await message.reply_text(
            f"🧠 <b>{target_name}</b> haqida hali hech narsa yodlanmagan.\n"
            f"Guruhda ko'proq yozsangiz, Misumi eslab qoladi! 😊",
            parse_mode="HTML"
        )
        return

    lines = [f"🧠 <b>{target_name}</b> haqida biladiganlarim:\n"]
    for k, v in facts.items():
        lines.append(f"  • <b>{k}</b>: {v}")
    lines.append("\n<i>Bu ma'lumotlar ship da ishlatiladi!</i>")

    await message.reply_text("\n".join(lines), parse_mode="HTML")


# ── /shipmemory — a'zo o'z ma'lumotini qo'shish ──────────────────────────────

async def shipmemory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /shipmemory kasb: dasturchi
    /shipmemory shahar: Toshkent
    Foydalanuvchi o'z ma'lumotini qo'lda kiritadi
    """
    chat = update.effective_chat
    message = update.message
    user = update.effective_user

    if chat.type not in ("group", "supergroup"):
        await message.reply_text("❌ Bu komanda faqat guruhlarda ishlaydi!")
        return

    if not context.args:
        await message.reply_text(
            "📝 <b>Foydalanish:</b> /shipmemory kalit: qiymat\n\n"
            "Misol:\n"
            "  /shipmemory kasb: dasturchi\n"
            "  /shipmemory shahar: Samarqand\n"
            "  /shipmemory sevimli_narsa: kitob o'qish\n\n"
            "<i>Bu ma'lumot keyingi /ship da ishlatiladi!</i>",
            parse_mode="HTML"
        )
        return

    raw = " ".join(context.args)
    if ":" not in raw:
        await message.reply_text(
            "❌ Format noto'g'ri. Misol: /shipmemory kasb: dasturchi",
            parse_mode="HTML"
        )
        return

    key, _, value = raw.partition(":")
    key = key.strip().lower().replace(" ", "_")
    value = value.strip()

    if not key or not value:
        await message.reply_text("❌ Kalit yoki qiymat bo'sh bo'lmasin!")
        return

    save_member_facts(chat.id, user.id, {key: value})
    await message.reply_text(
        f"✅ Yodlab qo'ydim!\n"
        f"  <b>{key}</b>: {value}\n\n"
        f"Keyingi /ship da shu ma'lumot ishlatiladi 🧠",
        parse_mode="HTML"
    )


# ── passiv a'zo yig'uvchi + so'z yodlash ─────────────────────────────────────

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

    # Xabardan faktlarni yodlash (30% ehtimollik — har xabarda emas)
    text = msg.text or ""
    if text and len(text) > 15 and random.random() < 0.30:
        try:
            facts = _extract_facts_from_text(text)
            if facts:
                save_member_facts(chat.id, u.id, facts)
                print(f"[ship:memory] {u.id} ({u.first_name}): {facts}")
        except Exception as e:
            print(f"[ship:memory:error] {e}")


# ── ro'yxatdan o'tkazish ──────────────────────────────────────────────────────

def register(app: Application) -> None:
    app.add_handler(CommandHandler("ship", ship_cmd))
    app.add_handler(CommandHandler("shipleader", shipleader_cmd))
    app.add_handler(CommandHandler("shipfact", shipfact_cmd))
    app.add_handler(CommandHandler("shipmemory", shipmemory_cmd))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _track_member),
        group=2,
    )
