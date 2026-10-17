"""
Misumi AI — extras.py

Yangi imkoniyatlar (bot.py ga 3 ta kichik o'zgarish bilan ulanadi):

✨ Premium (custom) emojilar
   /addemoji <premium emoji(lar)>  — (admin) premium emojini botga o'rgatish
   /delemoji <emoji>               — (admin) o'chirish
   /emojis                         — (admin) o'rgatilganlar ro'yxati
   /emojiid                        — premium emojining ID sini ko'rsatadi
   Bot yuborgan hamma xabardagi oddiy emojilar avtomatik premium'ga almashadi
   (agar o'rgatilgan bo'lsa). Premium ishlamasa — oddiy emoji bilan yuboriladi.

🚀 Yangi /start  — chiroyli karta, tugmalar (premium ikonkali), buyruqlar menyusi

🎨 /imagine <tavsif> — rasm chizish (Gemini, o'xshamasa Pollinations'ga o'tadi)

🎙 Ovozli suhbat — ovozli xabar yuborsang, bot tushunib ovozda javob beradi
🖼 Rasm tahlili  — rasm yubor (tushuntirish, roast, anime personaj, matnni o'qish)
🎬 /anime [nom]  — anime ma'lumoti (Jikan/MyAnimeList), nomsiz — tasodifiy

🔊 /ovoz [matn]  — matnni ovozga aylantirish (Edge TTS, o'zbek tili bor)
   /ovozsoz       — ovoz turini tanlash
   Shaxsiy chatda AI javobi tagida 🔊 tugma chiqadi.
"""
import asyncio
import html
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone

import requests
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageEntity,
    Update,
    WebAppInfo,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import admin_store
import ai_core
import db

logger = logging.getLogger("misumi-extras")

ADMIN_ID = os.environ.get("ADMIN_ID", "").strip()
WEBAPP_URL = os.environ.get("WEBAPP_URL") or os.environ.get("RENDER_EXTERNAL_URL")

IMAGINE_DAILY_LIMIT = int(os.environ.get("IMAGINE_DAILY_LIMIT", "15"))   # 0 = cheksiz
IMAGINE_COOLDOWN = int(os.environ.get("IMAGINE_COOLDOWN", "20"))          # soniya
TTS_MAX_CHARS = int(os.environ.get("TTS_MAX_CHARS", "1000"))

_VS16 = "\ufe0f"


def _esc(text: str) -> str:
    """Telegram HTML uchun: faqat < > & ni almashtiradi."""
    return html.escape(text, quote=False)


def _is_admin(user_id) -> bool:
    return bool(ADMIN_ID) and str(user_id) == ADMIN_ID


# ═══════════════════════════════════════════════════════════════════════════
# ✨ PREMIUM EMOJI
# ═══════════════════════════════════════════════════════════════════════════
# Baza: store="premium_emoji", key="map" -> {"🔥": "5368324170671202286", ...}
# Kalit — oddiy (fallback) emoji, qiymat — custom_emoji_id.

_EMOJI_STORE = "premium_emoji"
_emoji_cache: dict | None = None
_emoji_re: re.Pattern | None = None
_TG_EMOJI_RE = re.compile(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', re.S)
MAX_PREMIUM_PER_MESSAGE = 40


def _norm(ch: str) -> str:
    return ch.replace(_VS16, "")


def _emoji_map() -> dict:
    global _emoji_cache
    if _emoji_cache is None:
        try:
            _emoji_cache = dict(db.load(_EMOJI_STORE, "map", {}))
        except Exception:
            logger.exception("premium emoji xaritasini o'qib bo'lmadi")
            _emoji_cache = {}
        _rebuild_re()
    return _emoji_cache


def _rebuild_re() -> None:
    global _emoji_re
    keys = sorted((_emoji_cache or {}).keys(), key=len, reverse=True)
    _emoji_re = (
        re.compile("(" + "|".join(re.escape(k) for k in keys) + ")" + _VS16 + "?")
        if keys
        else None
    )


def _save_emoji_map() -> None:
    db.save(_EMOJI_STORE, "map", _emoji_cache or {})
    _rebuild_re()


def emoji_id(ch: str) -> str | None:
    return _emoji_map().get(_norm(ch))


def pe(ch: str) -> str:
    """HTML ichida ishlatiladigan emoji: o'rgatilgan bo'lsa premium, bo'lmasa oddiy."""
    eid = emoji_id(ch)
    if eid:
        return f'<tg-emoji emoji-id="{eid}">{ch}</tg-emoji>'
    return ch


def strip_premium(text: str) -> str:
    """<tg-emoji> teglarini oddiy emojiga qaytaradi (fallback uchun)."""
    return _TG_EMOJI_RE.sub(r"\1", text)


def premiumize(text: str) -> str | None:
    """Oddiy matndagi emojilarni premium'ga almashtiradi va HTML qaytaradi.
    O'rgatilgan emoji yo'q yoki matnda uchramasa — None (oddiy yuboriladi)."""
    _emoji_map()
    if not _emoji_re or not text:
        return None
    escaped = html.escape(text, quote=False)
    count = 0

    def _sub(m: re.Match) -> str:
        nonlocal count
        eid = _emoji_cache.get(m.group(1))
        if not eid or count >= MAX_PREMIUM_PER_MESSAGE:
            return m.group(0)
        count += 1
        return f'<tg-emoji emoji-id="{eid}">{m.group(1)}</tg-emoji>'

    out = _emoji_re.sub(_sub, escaped)
    return out if count else None


def icon_btn(label: str, ch: str, **kwargs) -> InlineKeyboardButton:
    """Tugma: premium ikonka bor bo'lsa — ikonka bilan, bo'lmasa oddiy emoji matn ichida."""
    eid = emoji_id(ch)
    if eid:
        try:
            return InlineKeyboardButton(label, icon_custom_emoji_id=eid, **kwargs)
        except TypeError:
            pass
    return InlineKeyboardButton(f"{ch} {label}", **kwargs)


def plain_btn(label: str, ch: str, **kwargs) -> InlineKeyboardButton:
    return InlineKeyboardButton(f"{ch} {label}", **kwargs)


async def reply_html(message, text: str, kb_factory=None, **kwargs):
    """HTML xabar yuboradi. Premium emoji/ikonka xato bersa — oddiy ko'rinishda qayta yuboradi.
    kb_factory(premium: bool) -> InlineKeyboardMarkup | None"""
    attempts = (True, False) if _emoji_map() else (False,)
    last = None
    for premium in attempts:
        t = text if premium else strip_premium(text)
        kb = kb_factory(premium) if kb_factory else None
        try:
            return await message.reply_text(t, parse_mode="HTML", reply_markup=kb, **kwargs)
        except BadRequest as e:
            last = e
            logger.warning("premium emoji bilan yuborib bo'lmadi (%s) — oddiy variantga o'tilyapti", e)
    raise last  # type: ignore[misc]


async def edit_html(query, text: str, kb_factory=None):
    attempts = (True, False) if _emoji_map() else (False,)
    last = None
    for premium in attempts:
        t = text if premium else strip_premium(text)
        kb = kb_factory(premium) if kb_factory else None
        try:
            return await query.edit_message_text(t, parse_mode="HTML", reply_markup=kb)
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return None
            last = e
    raise last  # type: ignore[misc]


async def send_ai_reply(update: Update, text: str) -> None:
    """AI javobini yuboradi: oddiy emojilar premium'ga almashadi, shaxsiy chatda 🔊 tugma qo'shiladi."""
    message = update.effective_message
    in_private = update.effective_chat and update.effective_chat.type == "private"
    markup = _voice_markup() if in_private and 8 <= len(text) <= TTS_MAX_CHARS else None

    rich = premiumize(text)
    if rich:
        try:
            await message.reply_text(rich, parse_mode="HTML", reply_markup=markup)
            return
        except BadRequest as e:
            logger.warning("premium AI javob yuborilmadi (%s) — oddiy yuborilyapti", e)
    await message.reply_text(text, reply_markup=markup)


async def cmd_addemoji(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return
    found = msg.parse_entities(types=[MessageEntity.CUSTOM_EMOJI])
    # reply qilingan xabardagi premium emojilarni ham olamiz
    if msg.reply_to_message:
        found.update(msg.reply_to_message.parse_entities(types=[MessageEntity.CUSTOM_EMOJI]))
    if not found:
        await msg.reply_text(
            "premium emoji topilmadi.\n\n"
            "/addemoji dan keyin premium emojilarni yoz (bir nechta bo'lishi mumkin), "
            "yoki premium emoji bor xabarga reply qilib /addemoji yoz."
        )
        return
    _emoji_map()
    added = []
    for ent, ch in found.items():
        if not ent.custom_emoji_id:
            continue
        _emoji_cache[_norm(ch)] = ent.custom_emoji_id
        added.append(ch)
    _save_emoji_map()
    await msg.reply_text(
        f"✅ {len(added)} ta premium emoji o'rgatildi: {' '.join(added)}\n"
        "Endi botdagi shu oddiy emojilar o'rniga premium variant chiqadi."
    )


async def cmd_delemoji(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return
    args = " ".join(context.args) if context.args else ""
    if not args:
        await msg.reply_text("Ishlatilishi: /delemoji 🔥")
        return
    _emoji_map()
    removed = 0
    for ch in list(_emoji_cache.keys()):
        if ch in _norm(args):
            del _emoji_cache[ch]
            removed += 1
    if removed:
        _save_emoji_map()
    await msg.reply_text(f"🗑 {removed} ta o'chirildi." if removed else "Bunday emoji o'rgatilmagan.")


async def cmd_emojis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return
    m = _emoji_map()
    if not m:
        await msg.reply_text("Hali premium emoji o'rgatilmagan. /addemoji dan foydalan.")
        return
    line = " ".join(m.keys())
    await reply_html(msg, f"Premium o'rgatilgan ({len(m)} ta):\n" + " ".join(pe(k) for k in m.keys()) + f"\n\nOddiy ko'rinishi: {_esc(line)}")


async def cmd_emojiid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    found = msg.parse_entities(types=[MessageEntity.CUSTOM_EMOJI])
    if msg.reply_to_message:
        found.update(msg.reply_to_message.parse_entities(types=[MessageEntity.CUSTOM_EMOJI]))
    if not found:
        await msg.reply_text("Premium emoji yubor yoki unga reply qilib /emojiid yoz.")
        return
    lines = [f"{ch}  <code>{ent.custom_emoji_id}</code>" for ent, ch in found.items()]
    await msg.reply_text("\n".join(lines), parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════════════
# 🚀 /start
# ═══════════════════════════════════════════════════════════════════════════

def _start_text(first_name: str | None) -> str:
    name = _esc(first_name) if first_name else "do'st"
    return (
        f"{pe('✨')} <b>Salom, {name}!</b>\n"
        f"Men <b>{_esc(ai_core.BOT_NAME)}</b>man.\n\n"
        f"{pe('💬')} Gaplashaman: oddiy yoz, javob beraman\n"
        f"{pe('🎙')} Ovozli xabar yubor, ovozda javob beraman\n"
        f"{pe('🖼')} Rasm yubor: tushuntiraman, roast qilaman\n"
        f"{pe('🎨')} Rasm chizaman: <code>/imagine</code>\n"
        f"{pe('🎬')} Anime qidiraman: <code>/anime Naruto</code>\n"
        f"{pe('🎲')} Guruhda o'yinlar: duel, ship, viktorina...\n\n"
        "<blockquote>Pastdagi tugmalardan boshla, yoki shunchaki yoz.</blockquote>"
    )


def _start_kb(chat_id, bot_username: str | None, premium: bool) -> InlineKeyboardMarkup:
    mk = icon_btn if premium else plain_btn
    rows = []
    if WEBAPP_URL:
        rows.append([mk(f"{ai_core.BOT_NAME} ni ochish", "✨", web_app=WebAppInfo(url=WEBAPP_URL))])
    rows.append([
        mk("Buyruqlar", "📋", callback_data="st:cmds"),
        mk("Hisob", "👤", callback_data="ub:account"),
    ])
    if bot_username:
        rows.append([mk("Guruhga qo'shish", "➕", url=f"https://t.me/{bot_username}?startgroup=true")])
    if WEBAPP_URL and _is_admin(chat_id):
        rows.append([mk("Admin panel", "⚙️", web_app=WebAppInfo(url=f"{WEBAPP_URL.rstrip('/')}/admin"))])
    return InlineKeyboardMarkup(rows)


async def send_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user
    text = _start_text(user.first_name if user else None)
    bot_username = context.bot.username
    await reply_html(
        update.effective_message, text,
        kb_factory=lambda prem: _start_kb(chat_id, bot_username, prem),
    )


_CMDS_PAGES = {
    "games": (
        "🎮", "O'yinlar",
        "/duel — birovga (yoki menga) o'yin taklif qilish\n"
        "/tictactoe — X-O\n"
        "/quiz (/viktorina) — guruh viktorinasi\n"
        "/rps — tosh-qaychi-qog'oz\n"
        "/truth · /dare · /wouldyourather\n"
        "/reyting — o'yin reytingi",
    ),
    "fun": (
        "🎲", "Fun",
        "/ship · /shipleader · /shipfact — juftlik tanlash\n"
        "/marry · /couple · /soulmate · /crush\n"
        "/roast · /compliment · /rate\n"
        "/8ball · /horoscope · /lucky · /quote\n"
        "/confession · /ngl · /fakechat",
    ),
    "tools": (
        "🛠", "Foydali",
        "/imagine — rasm chizish\n"
        "/anime — anime qidirish (nomsiz — tasodifiy)\n"
        "/ovoz — matnni ovozga aylantirish\n"
        "/ovozsoz — ovoz turini tanlash\n"
        "Ovozli xabar yubor — ovozda javob beraman\n"
        "Rasm yubor — tushuntiraman, roast qilaman\n"
        "/translate — tarjima\n"
        "/weather — ob-havo\n"
        "/calc — kalkulyator\n"
        "/remind — eslatma\n"
        "/stats · /who · /activity — guruh statistikasi\n"
        "/stiker — stikerni turkumga qo'shish\n"
        "/reset — xotirani tozalash",
    ),
}


def _cmds_text(page: str) -> str:
    ch, title, body = _CMDS_PAGES[page]
    return f"{pe(ch)} <b>{title}</b>\n\n<blockquote>{_esc(body)}</blockquote>"


def _cmds_kb(page: str, premium: bool) -> InlineKeyboardMarkup:
    mk = icon_btn if premium else plain_btn
    tabs = [
        mk(t, c, callback_data=f"st:p:{k}") if k != page else InlineKeyboardButton(f"• {t}", callback_data=f"st:p:{k}")
        for k, (c, t, _b) in _CMDS_PAGES.items()
    ]
    return InlineKeyboardMarkup([tabs, [mk("Orqaga", "⬅️", callback_data="st:home")]])


async def start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    chat_id = query.message.chat.id if query.message else update.effective_chat.id
    if data == "st:home":
        text = _start_text(query.from_user.first_name if query.from_user else None)
        bu = context.bot.username
        await edit_html(query, text, lambda prem: _start_kb(chat_id, bu, prem))
    elif data == "st:cmds" or data.startswith("st:p:"):
        page = data.split(":")[2] if data.startswith("st:p:") else "games"
        if page not in _CMDS_PAGES:
            page = "games"
        await edit_html(query, _cmds_text(page), lambda prem: _cmds_kb(page, prem))


# ═══════════════════════════════════════════════════════════════════════════
# 🎨 /imagine
# ═══════════════════════════════════════════════════════════════════════════

_imagine_last: dict[int, float] = {}
_imagine_count: dict[tuple, int] = {}
_imagine_prompts: dict[str, str] = {}      # regen tugmasi uchun (RAM)
_pollinations_lock = asyncio.Lock()
_pollinations_last = 0.0
POLLINATIONS_MIN_GAP = 16.0                 # anonim tarif: 15 soniyada 1 so'rov


def _tashkent_day() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=5)).strftime("%Y-%m-%d")


def _imagine_allowed(user_id: int) -> tuple[bool, str]:
    if _is_admin(user_id):
        return True, ""
    now = time.time()
    wait = IMAGINE_COOLDOWN - (now - _imagine_last.get(user_id, 0))
    if wait > 0:
        return False, f"sekin, yana {int(wait) + 1} soniya kut"
    if IMAGINE_DAILY_LIMIT > 0 and _imagine_count.get((user_id, _tashkent_day()), 0) >= IMAGINE_DAILY_LIMIT:
        return False, f"bugungi limit ({IMAGINE_DAILY_LIMIT} ta rasm) tugadi, ertaga qayt"
    return True, ""


def _imagine_mark(user_id: int) -> None:
    _imagine_last[user_id] = time.time()
    k = (user_id, _tashkent_day())
    _imagine_count[k] = _imagine_count.get(k, 0) + 1
    if len(_imagine_count) > 5000:                     # eski kunlarni tozalash
        today = _tashkent_day()
        for key in [k2 for k2 in _imagine_count if k2[1] != today]:
            _imagine_count.pop(key, None)


def _to_english(prompt: str) -> str:
    """Pollinations (Flux) inglizcha promptda yaxshi ishlaydi. Tarjima qila olmasak — asl matn."""
    system = (
        "Translate the user's image description into a vivid, concise English prompt for an "
        "image generator. Output ONLY the English prompt, no quotes, no explanations."
    )
    for call in (ai_core._call_cerebras, ai_core._call_gemini, ai_core._call_groq):
        try:
            out = call(system, [], prompt).strip().strip('"')
            if out:
                return out[:500]
        except Exception:
            continue
    return prompt


def _pollinations_sync(prompt: str) -> bytes:
    from urllib.parse import quote

    url = f"https://image.pollinations.ai/prompt/{quote(_to_english(prompt))}"
    params = {"model": "flux", "width": 1024, "height": 1024, "nologo": "true", "seed": random.randint(1, 10_000_000)}
    r = requests.get(url, params=params, timeout=90)
    r.raise_for_status()
    if not r.headers.get("content-type", "").startswith("image"):
        raise RuntimeError("Pollinations rasm qaytarmadi")
    return r.content


async def _generate(prompt: str) -> tuple[bytes, str]:
    """(rasm, provider). Avval Gemini, bo'lmasa Pollinations."""
    global _pollinations_last
    try:
        data, _mime = await asyncio.to_thread(ai_core._call_gemini_image, prompt)
        return data, "gemini"
    except Exception as e:
        logger.warning("Gemini rasm chizmadi (%s) — Pollinations'ga o'tilyapti", e)
    async with _pollinations_lock:
        gap = POLLINATIONS_MIN_GAP - (time.time() - _pollinations_last)
        if gap > 0:
            await asyncio.sleep(gap)
        try:
            data = await asyncio.to_thread(_pollinations_sync, prompt)
        finally:
            _pollinations_last = time.time()
    return data, "pollinations"


async def _do_imagine(message, user, chat, prompt: str, context) -> None:
    ok, why = _imagine_allowed(user.id)
    if not ok:
        await message.reply_text(why)
        return
    if admin_store.is_blocked(chat.id):
        return
    _imagine_mark(user.id)
    await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.UPLOAD_PHOTO)
    status = await message.reply_text(f"🎨 chizyapman... (30-60 soniya olishi mumkin)")
    try:
        admin_store.record_message(str(user.id), name=user.first_name, source="telegram")
    except Exception:
        pass
    try:
        data, provider = await _generate(prompt)
    except Exception:
        logger.exception("/imagine xato")
        await status.edit_text("rasm chiqmadi, server band yoki tavsif mos emas. keyinroq ur.")
        return
    key = f"{user.id}{int(time.time())}"[-12:]
    _imagine_prompts[key] = prompt
    if len(_imagine_prompts) > 500:
        for k in list(_imagine_prompts)[:100]:
            _imagine_prompts.pop(k, None)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Yana", callback_data=f"img:re:{key}")]])
    caption = _esc(prompt[:200])
    try:
        await message.reply_photo(photo=data, caption=f"🎨 <i>{caption}</i>", parse_mode="HTML", reply_markup=kb)
    finally:
        try:
            await status.delete()
        except TelegramError:
            pass


async def cmd_imagine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not user or not chat:
        return
    prompt = " ".join(context.args).strip() if context.args else ""
    if not prompt and msg.reply_to_message and msg.reply_to_message.text:
        prompt = msg.reply_to_message.text.strip()
    if not prompt:
        await msg.reply_text("nima chizay? masalan:\n/imagine kechki Toshkent, anime uslubida")
        return
    await _do_imagine(msg, user, chat, prompt[:600], context)


async def imagine_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        await query.answer()
        return
    prompt = _imagine_prompts.get(parts[2])
    if not prompt:
        await query.answer("eskirib qoldi, /imagine ni qaytadan yoz", show_alert=True)
        return
    ok, why = _imagine_allowed(query.from_user.id)
    if not ok:
        await query.answer(why, show_alert=True)
        return
    await query.answer("chizyapman...")
    await _do_imagine(query.message, query.from_user, query.message.chat, prompt, context)


# ═══════════════════════════════════════════════════════════════════════════
# 🔊 TTS (ovoz)
# ═══════════════════════════════════════════════════════════════════════════

VOICES = {
    "uz_f": ("uz-UZ-MadinaNeural", "🇺🇿 Madina (ayol)"),
    "uz_m": ("uz-UZ-SardorNeural", "🇺🇿 Sardor (erkak)"),
    "ru": ("ru-RU-SvetlanaNeural", "🇷🇺 Svetlana"),
    "en": ("en-US-AriaNeural", "🇺🇸 Aria"),
}
_TTS_STORE = "tts_voice"
_tts_last: dict[int, float] = {}
TTS_COOLDOWN = 5

_MD_RE = re.compile(r"[*_`#>~|]+")
_URL_RE = re.compile(r"https?://\S+")
_EMOJI_CLEAN_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\u200d\ufe0f]+"
)


def _clean_for_tts(text: str) -> str:
    text = _URL_RE.sub(" ", text)
    text = _MD_RE.sub(" ", text)
    text = _EMOJI_CLEAN_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _get_voice_key(user_id: int) -> str:
    try:
        k = db.load(_TTS_STORE, str(user_id), "uz_f")
    except Exception:
        k = "uz_f"
    return k if k in VOICES else "uz_f"


def _set_voice_key(user_id: int, key: str) -> None:
    try:
        db.save(_TTS_STORE, str(user_id), key)
    except Exception:
        logger.exception("ovoz sozlamasini saqlab bo'lmadi")


async def _synthesize(text: str, voice: str) -> bytes:
    import edge_tts

    buf = bytearray()
    async for chunk in edge_tts.Communicate(text, voice).stream():
        if chunk["type"] == "audio":
            buf += chunk["data"]
    if not buf:
        raise RuntimeError("TTS bo'sh javob qaytardi")
    return bytes(buf)


def _voice_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔊 Ovozda", callback_data="tts:say")]])


async def _speak(message, user_id: int, raw_text: str, context) -> None:
    text = _clean_for_tts(raw_text)[:TTS_MAX_CHARS]
    if len(text) < 2:
        await message.reply_text("o'qiydigan matn topilmadi.")
        return
    if not _is_admin(user_id):
        wait = TTS_COOLDOWN - (time.time() - _tts_last.get(user_id, 0))
        if wait > 0:
            await message.reply_text(f"sekin, {int(wait) + 1} soniya kut")
            return
    _tts_last[user_id] = time.time()
    await context.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.RECORD_VOICE)
    voice = VOICES[_get_voice_key(user_id)][0]
    try:
        audio = await asyncio.wait_for(_synthesize(text, voice), timeout=45)
    except Exception:
        logger.exception("TTS xato")
        await message.reply_text("ovoz chiqmadi, keyinroq ur.")
        return
    await message.reply_voice(voice=audio)


async def cmd_ovoz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    text = " ".join(context.args).strip() if context.args else ""
    if not text and msg.reply_to_message:
        text = (msg.reply_to_message.text or msg.reply_to_message.caption or "").strip()
    if not text:
        await msg.reply_text("Matn yoz yoki xabarga reply qil:\n/ovoz salom, qalaysan?\n\nOvoz turi: /ovozsoz")
        return
    await _speak(msg, user.id, text, context)


def _voice_pick_markup(current: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(("✅ " if k == current else "") + label, callback_data=f"tts:set:{k}")]
        for k, (_v, label) in VOICES.items()
    ]
    return InlineKeyboardMarkup(rows)


async def cmd_ovozsoz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.effective_message.reply_text(
        "Qaysi ovozda gapiray?", reply_markup=_voice_pick_markup(_get_voice_key(user.id))
    )


async def tts_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    user = query.from_user
    if data == "tts:say":
        text = (query.message.text or query.message.caption or "") if query.message else ""
        if not text:
            await query.answer("matn topilmadi", show_alert=True)
            return
        await query.answer("o'qiyapman...")
        await _speak(query.message, user.id, text, context)
    elif data.startswith("tts:set:"):
        key = data.split(":")[2]
        if key not in VOICES:
            await query.answer()
            return
        _set_voice_key(user.id, key)
        await query.answer("ovoz tanlandi")
        try:
            await query.edit_message_reply_markup(reply_markup=_voice_pick_markup(key))
        except BadRequest:
            pass
    else:
        await query.answer()


# ═══════════════════════════════════════════════════════════════════════════
# 🧰 Umumiy yordamchilar (AI bir martalik chaqiruv, guruhda javob berish sharti)
# ═══════════════════════════════════════════════════════════════════════════

def _ai_oneshot(system: str, prompt: str, fallback: str = "") -> str:
    """Persona/xotirasiz bir martalik AI chaqiruvi (tarjima, formatlash uchun)."""
    for call in (ai_core._call_cerebras, ai_core._call_gemini, ai_core._call_groq):
        try:
            out = call(system, [], prompt).strip().strip('"')
            if out:
                return out
        except Exception:
            continue
    return fallback


def _should_answer(update: Update, bot_username: str | None, caption_ok: bool = True) -> bool:
    """Shaxsiy chatda — doim. Guruhda — faqat botning xabariga reply qilinsa yoki
    sarlavhada 'misumi' / @bot yozilgan bo'lsa."""
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or not msg:
        return False
    if chat.type == "private":
        return True
    r = msg.reply_to_message
    if r and r.from_user and r.from_user.is_bot and bot_username and r.from_user.username == bot_username:
        return True
    cap = (msg.caption or "").lower() if caption_ok else ""
    if "misumi" in cap or (bot_username and f"@{bot_username.lower()}" in cap):
        return True
    return False


def _gate(update: Update) -> bool:
    """Bloklangan chat / texnik ishlar / tezlik cheklovi. False bo'lsa — javob berilmaydi."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return False
    if admin_store.is_blocked(chat.id):
        return False
    if admin_store.is_maintenance() and not _is_admin(user.id):
        return False
    if not ai_core.check_rate_limit(chat.id, user.id):
        return False
    return True


# ═══════════════════════════════════════════════════════════════════════════
# 🎙 OVOZLI SUHBAT: ovoz yubor -> bot tushunadi -> ovozda javob beradi
# ═══════════════════════════════════════════════════════════════════════════

VOICE_MAX_SECONDS = 90
_VOICE_HINT = (
    "Foydalanuvchi ovozli xabar yubordi va javobing ovoz bilan o'qib beriladi. "
    "Shuning uchun qisqa (1-3 gap), og'zaki tilda yoz. Emoji, markdown, ro'yxat, havola ishlatma."
)
_CYR_RE = re.compile(r"[А-Яа-яЁё]")


def _transcribe_sync(audio: bytes, mime: str) -> str:
    from google.genai import types

    resp = ai_core._gemini_client.models.generate_content(
        model=ai_core.GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=audio, mime_type=mime),
            "Bu ovozli xabarni so'zma-so'z matnga aylantir (o'zbek, rus yoki ingliz tilida bo'lishi mumkin). "
            "Faqat aytilgan gapning o'zini yoz, izoh yoki tarjima qo'shma. Gap aniq eshitilmasa, hech narsa yozma.",
        ],
    )
    return (resp.text or "").strip()


def _pick_voice_for_text(user_id: int, text: str) -> str:
    key = _get_voice_key(user_id)
    letters = [c for c in text if c.isalpha()]
    if letters and len(_CYR_RE.findall(text)) / len(letters) > 0.5 and key != "ru":
        return "ru"
    return key


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    media = msg.voice or msg.audio if msg else None
    if not media or not user or not chat:
        return
    if not _should_answer(update, context.bot.username, caption_ok=False):
        return
    if not _gate(update):
        return
    if (media.duration or 0) > VOICE_MAX_SECONDS:
        await msg.reply_text(f"uzun ekan, {VOICE_MAX_SECONDS} soniyadan qisqaroq yubor.")
        return

    await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.RECORD_VOICE)
    try:
        f = await media.get_file()
        audio = bytes(await f.download_as_bytearray())
        mime = getattr(media, "mime_type", None) or "audio/ogg"
        heard = await asyncio.wait_for(asyncio.to_thread(_transcribe_sync, audio, mime), timeout=60)
    except Exception:
        logger.exception("ovozni tushunib bo'lmadi")
        await msg.reply_text("ovozni eshita olmadim, qaytadan yubor yoki yoz.")
        return
    if not heard:
        await msg.reply_text("hech narsa eshitilmadi, ovozliroq ayt.")
        return

    try:
        reply = await asyncio.to_thread(
            ai_core.get_ai_reply, str(user.id), heard,
            name=user.first_name, source="telegram", extra_system=_VOICE_HINT,
        )
    except Exception:
        logger.exception("ovozli suhbat: AI xato")
        reply = ""
    if not reply or reply == "...":
        await msg.reply_text("miya ishlamayapti hozir, keyinroq gap.")
        return

    spoken = _clean_for_tts(reply)[:TTS_MAX_CHARS]
    if len(spoken) < 2:
        await send_ai_reply(update, reply)
        return
    await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.RECORD_VOICE)
    try:
        voice_key = _pick_voice_for_text(user.id, spoken)
        audio_out = await asyncio.wait_for(_synthesize(spoken, VOICES[voice_key][0]), timeout=45)
        await msg.reply_voice(voice=audio_out, caption=reply[:1000])
    except Exception:
        logger.exception("ovozli javob chiqmadi — matn bilan javob beriladi")
        await send_ai_reply(update, reply)


# ═══════════════════════════════════════════════════════════════════════════
# 🖼 RASM TAHLILI
# ═══════════════════════════════════════════════════════════════════════════

_photo_cache: dict[str, str] = {}      # qisqa kalit -> telegram file_id (RAM)

_PHOTO_MODES = {
    "explain": (
        "🔍", "Tushuntir",
        "Bu rasmda nima bor? Qisqa va qiziqarli tushuntir.",
        "",
    ),
    "roast": (
        "🔥", "Roast",
        "Bu rasmni do'stona kulgili roast qil.",
        "Roast do'stona va kulgili bo'lsin: rasmdagi vaziyat, kiyim, muhit, kayfiyatga qarab hazil qil. "
        "Tashqi ko'rinish, tana, millat, din yoki jins haqida kamsituvchi gap yo'q. 3-4 gap.",
    ),
    "anime": (
        "🎭", "Qaysi anime personaj?",
        "Rasmdagi odam yoki narsa qaysi anime personajga o'xshaydi?",
        "Eng o'xshash anime personajni top: ismi va qaysi animedan ekanini ayt, nega o'xshashini kulgili "
        "tushuntir. Rasmdagi odamning kimligini aniqlashga urinma, faqat umumiy ko'rinish, kayfiyat va vibega "
        "qarab o'xshat. 3-4 gap.",
    ),
    "ocr": (
        "📝", "Matnni o'qi",
        "Rasmdagi matnni aynan ko'chirib yoz.",
        "Rasmdagi matnni o'zgartirmasdan yoz. Tarjima qilma. Matn bo'lmasa, shunday de.",
    ),
}


def _photo_menu_kb(key: str, premium: bool) -> InlineKeyboardMarkup:
    mk = icon_btn if premium else plain_btn
    items = [mk(label, ch, callback_data=f"ph:{mode}:{key}") for mode, (ch, label, _p, _s) in _PHOTO_MODES.items()]
    return InlineKeyboardMarkup([items[:2], items[2:]])


async def _analyze(update_or_msg_user, user, photo_bytes: bytes, prompt: str, extra: str) -> str:
    return await asyncio.to_thread(
        ai_core.get_ai_reply, str(user.id), prompt,
        image_bytes=photo_bytes, image_mime="image/jpeg",
        name=user.first_name, source="telegram", extra_system=extra or None,
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not msg.photo or not user or not chat:
        return
    if not _should_answer(update, context.bot.username):
        return
    caption = (msg.caption or "").strip()

    # Sarlavhasiz rasm (shaxsiy chatda) — nima qilishni so'raymiz
    if not caption:
        if chat.type != "private":
            return
        if admin_store.is_blocked(chat.id):
            return
        key = f"{user.id}{msg.message_id}"[-12:]
        _photo_cache[key] = msg.photo[-1].file_id
        if len(_photo_cache) > 500:
            for k in list(_photo_cache)[:100]:
                _photo_cache.pop(k, None)
        await reply_html(
            msg, f"{pe('🖼')} Rasm keldi. Nima qilay?",
            kb_factory=lambda prem: _photo_menu_kb(key, prem),
        )
        return

    if not _gate(update):
        return
    await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
    try:
        f = await msg.photo[-1].get_file()
        data = bytes(await f.download_as_bytearray())
        reply = await _analyze(None, user, data, caption, "")
    except Exception:
        logger.exception("rasm tahlili xato")
        await msg.reply_text("rasmni ko'ra olmadim, keyinroq ur.")
        return
    if not reply or reply == "...":
        reply = "rasmni ko'ra olmadim, keyinroq ur."
    await send_ai_reply(update, reply)


async def photo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[1] not in _PHOTO_MODES:
        await query.answer()
        return
    file_id = _photo_cache.get(parts[2])
    if not file_id:
        await query.answer("rasm eskirib qoldi, qaytadan yubor", show_alert=True)
        return
    user = query.from_user
    chat = query.message.chat
    if admin_store.is_blocked(chat.id):
        await query.answer()
        return
    if not ai_core.check_rate_limit(chat.id, user.id):
        await query.answer("sekin, bir nafas ol", show_alert=True)
        return
    await query.answer("ko'ryapman...")
    _ch, _label, prompt, extra = _PHOTO_MODES[parts[1]]
    await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
    try:
        f = await context.bot.get_file(file_id)
        data = bytes(await f.download_as_bytearray())
        reply = await _analyze(None, user, data, prompt, extra)
    except Exception:
        logger.exception("rasm tahlili (tugma) xato")
        await query.message.reply_text("rasmni ko'ra olmadim, keyinroq ur.")
        return
    if not reply or reply == "...":
        reply = "rasmni ko'ra olmadim, keyinroq ur."
    rich = premiumize(reply)
    try:
        if rich:
            await query.message.reply_text(rich, parse_mode="HTML")
        else:
            await query.message.reply_text(reply)
    except BadRequest:
        await query.message.reply_text(reply)


# ═══════════════════════════════════════════════════════════════════════════
# 🎬 /anime  (Jikan API — MyAnimeList, bepul, kalit shart emas)
# ═══════════════════════════════════════════════════════════════════════════

JIKAN = "https://api.jikan.moe/v4"
_jikan_lock = asyncio.Lock()
_jikan_last = 0.0
_jikan_cache: dict[str, tuple[float, dict]] = {}
_synopsis_uz: dict[int, str] = {}
JIKAN_CACHE_TTL = 600

_STATUS_UZ = {
    "Finished Airing": "Tugagan",
    "Currently Airing": "Efirda",
    "Not yet aired": "Hali chiqmagan",
}


def _jikan_get_sync(path: str, params: dict | None) -> dict:
    r = requests.get(
        f"{JIKAN}{path}", params=params, timeout=15,
        headers={"User-Agent": "MisumiAI-TelegramBot/1.0"},
    )
    if r.status_code == 429:
        time.sleep(1.5)
        r = requests.get(f"{JIKAN}{path}", params=params, timeout=15,
                         headers={"User-Agent": "MisumiAI-TelegramBot/1.0"})
    r.raise_for_status()
    return r.json()


async def _jikan(path: str, params: dict | None = None, cache: bool = True) -> dict:
    """Jikan so'rovi: keshlanadi va sekundiga ~2 tadan oshmaydi."""
    global _jikan_last
    ck = path + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
    hit = _jikan_cache.get(ck)
    if cache and hit and time.time() - hit[0] < JIKAN_CACHE_TTL:
        return hit[1]
    async with _jikan_lock:
        gap = 0.5 - (time.time() - _jikan_last)
        if gap > 0:
            await asyncio.sleep(gap)
        try:
            data = await asyncio.to_thread(_jikan_get_sync, path, params)
        finally:
            _jikan_last = time.time()
    if cache:
        if len(_jikan_cache) > 300:
            _jikan_cache.clear()
        _jikan_cache[ck] = (time.time(), data)
    return data


def _is_safe(a: dict) -> bool:
    rating = (a.get("rating") or "")
    return not rating.startswith("Rx")


async def _synopsis_to_uz(a: dict) -> str:
    mal_id = a.get("mal_id")
    if mal_id in _synopsis_uz:
        return _synopsis_uz[mal_id]
    syn = (a.get("synopsis") or "").split("[Written by")[0].strip()
    if not syn:
        return ""
    syn = syn[:600]
    uz = await asyncio.to_thread(
        _ai_oneshot,
        "Sen tarjimonsan. Anime tavsifini ravon o'zbek tiliga (lotin yozuvi) tarjima qil. "
        "Faqat tarjimani yoz, 2-3 gapdan oshirma, izoh qo'shma.",
        syn, syn,
    )
    if mal_id:
        _synopsis_uz[mal_id] = uz
    return uz


def _anime_caption(a: dict, synopsis: str) -> str:
    title = a.get("title_english") or a.get("title") or "?"
    jp = a.get("title_japanese")
    lines = [f"{pe('🎬')} <b>{_esc(title)}</b>"]
    if jp and jp != title:
        lines.append(f"<i>{_esc(jp)}</i>")
    facts = []
    if a.get("score"):
        facts.append(f"{pe('⭐')} {a['score']}" + (f" (#{a['rank']})" if a.get("rank") else ""))
    kind = a.get("type")
    eps = a.get("episodes")
    if kind:
        facts.append(f"{kind}" + (f", {eps} qism" if eps else ""))
    elif eps:
        facts.append(f"{eps} qism")
    if a.get("status"):
        facts.append(_STATUS_UZ.get(a["status"], a["status"]))
    year = a.get("year") or ((a.get("aired") or {}).get("prop", {}).get("from", {}) or {}).get("year")
    if year:
        facts.append(str(year))
    if facts:
        lines.append(" · ".join(facts))
    genres = ", ".join(g["name"] for g in (a.get("genres") or [])[:5])
    if genres:
        lines.append(f"{pe('🏷')} {_esc(genres)}")
    studios = ", ".join(s["name"] for s in (a.get("studios") or [])[:2])
    if studios:
        lines.append(f"{pe('🏢')} {_esc(studios)}")
    head = "\n".join(lines)
    if synopsis:
        room = 1000 - len(head) - 40
        if room > 80:
            body = _esc(synopsis)
            if len(body) > room:
                body = body[: room - 1].rsplit(" ", 1)[0] + "…"
            head += f"\n\n<blockquote>{body}</blockquote>"
    return head[:1020]


def _anime_kb(a: dict, others: list[dict] | None, premium: bool) -> InlineKeyboardMarkup:
    mk = icon_btn if premium else plain_btn
    rows = []
    links = []
    if a.get("url"):
        links.append(mk("MAL", "🔗", url=a["url"]))
    trailer = (a.get("trailer") or {}).get("url")
    if trailer:
        links.append(mk("Treyler", "▶️", url=trailer))
    if links:
        rows.append(links)
    for o in (others or [])[:4]:
        t = (o.get("title_english") or o.get("title") or "?")[:40]
        rows.append([InlineKeyboardButton(t, callback_data=f"an:id:{o['mal_id']}")])
    rows.append([mk("Tasodifiy anime", "🎲", callback_data="an:rand")])
    return InlineKeyboardMarkup(rows)


async def _send_anime_card(message, a: dict, others: list[dict] | None = None) -> None:
    synopsis = await _synopsis_to_uz(a)
    caption = _anime_caption(a, synopsis)
    img = ((a.get("images") or {}).get("jpg") or {}).get("large_image_url")
    attempts = (True, False) if _emoji_map() else (False,)
    last: Exception | None = None
    for premium in attempts:
        cap = caption if premium else strip_premium(caption)
        kb = _anime_kb(a, others, premium)
        try:
            if img:
                try:
                    await message.reply_photo(photo=img, caption=cap, parse_mode="HTML", reply_markup=kb)
                    return
                except BadRequest as e:
                    if "emoji" in str(e).lower() or "entit" in str(e).lower():
                        raise
                    logger.warning("anime rasmi yuborilmadi (%s) — matn bilan", e)
            await message.reply_text(cap, parse_mode="HTML", reply_markup=kb)
            return
        except BadRequest as e:
            last = e
    if last:
        raise last


async def _random_anime() -> dict | None:
    for _ in range(4):
        data = await _jikan("/random/anime", cache=False)
        a = data.get("data") or {}
        if a and _is_safe(a) and ((a.get("images") or {}).get("jpg") or {}).get("large_image_url"):
            return a
    return None


async def cmd_anime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or admin_store.is_blocked(chat.id):
        return
    query = " ".join(context.args).strip() if context.args else ""
    await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.UPLOAD_PHOTO)
    try:
        if not query:
            a = await _random_anime()
            if not a:
                await msg.reply_text("hozir topolmadim, yana ur.")
                return
            await _send_anime_card(msg, a)
            return
        data = await _jikan("/anime", {"q": query[:80], "limit": 6, "sfw": "true"})
        results = [r for r in (data.get("data") or []) if _is_safe(r)]
        if not results:
            await msg.reply_text("bunaqa anime topilmadi. nomini inglizcha yoki yaponcha (romaji) yozib ko'r.")
            return
        await _send_anime_card(msg, results[0], results[1:5])
    except Exception:
        logger.exception("/anime xato")
        await msg.reply_text("anime bazasi hozir javob bermayapti, birozdan keyin ur.")


async def anime_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    chat = query.message.chat if query.message else None
    if not chat or admin_store.is_blocked(chat.id):
        await query.answer()
        return
    await query.answer()
    await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.UPLOAD_PHOTO)
    try:
        if data == "an:rand":
            a = await _random_anime()
        elif data.startswith("an:id:") and data[6:].isdigit():
            a = (await _jikan(f"/anime/{data[6:]}")).get("data")
        else:
            return
        if not a:
            await query.message.reply_text("topolmadim, yana ur.")
            return
        await _send_anime_card(query.message, a)
    except Exception:
        logger.exception("anime callback xato")
        await query.message.reply_text("anime bazasi hozir javob bermayapti, birozdan keyin ur.")


# ═══════════════════════════════════════════════════════════════════════════
# Ro'yxatdan o'tkazish
# ═══════════════════════════════════════════════════════════════════════════

def register(app: Application) -> None:
    # group=0 — bot.py dagi asosiy handlerlar bilan birga, /start dan tashqari barchasi yangi
    app.add_handler(CommandHandler("imagine", cmd_imagine), group=0)
    app.add_handler(CommandHandler(["ovoz", "tts"], cmd_ovoz), group=0)
    app.add_handler(CommandHandler("ovozsoz", cmd_ovozsoz), group=0)
    app.add_handler(CommandHandler("addemoji", cmd_addemoji), group=0)
    app.add_handler(CommandHandler("delemoji", cmd_delemoji), group=0)
    app.add_handler(CommandHandler("emojis", cmd_emojis), group=0)
    app.add_handler(CommandHandler("emojiid", cmd_emojiid), group=0)
    app.add_handler(CallbackQueryHandler(start_callback, pattern=r"^st:"), group=0)
    app.add_handler(CallbackQueryHandler(imagine_callback, pattern=r"^img:"), group=0)
    app.add_handler(CallbackQueryHandler(tts_callback, pattern=r"^tts:"), group=0)
    app.add_handler(CommandHandler("anime", cmd_anime), group=0)
    app.add_handler(CallbackQueryHandler(anime_callback, pattern=r"^an:"), group=0)
    app.add_handler(CallbackQueryHandler(photo_callback, pattern=r"^ph:"), group=0)
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice), group=0)
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo), group=0)
