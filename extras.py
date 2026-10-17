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
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

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
    await reply_html(msg, f"Premium o'rgatilgan ({len(m)} ta):\n" + " ".join(pe(k) for k in m.keys()) + f"\n\nOddiy ko'rinishi: {html.escape(line)}")


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
    name = html.escape(first_name) if first_name else "do'st"
    return (
        f"{pe('✨')} <b>Salom, {name}!</b>\n"
        f"Men <b>{html.escape(ai_core.BOT_NAME)}</b>man.\n\n"
        f"{pe('💬')} Gaplashaman — oddiy yoz, javob beraman\n"
        f"{pe('🎨')} Rasm chizaman — <code>/imagine</code> yoki «rasm chiz...»\n"
        f"{pe('🔊')} Ovozda gapiraman — <code>/ovoz</code>\n"
        f"{pe('🎲')} Guruhda o'yin o'ynataman — duel, ship, viktorina...\n\n"
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
        "/ovoz — matnni ovozga aylantirish\n"
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
    return f"{pe(ch)} <b>{title}</b>\n\n<blockquote>{html.escape(body)}</blockquote>"


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
    caption = html.escape(prompt[:200])
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
