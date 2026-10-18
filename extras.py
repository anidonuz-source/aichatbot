"""
Misumi AI — extras.py

Yangi imkoniyatlar (bot.py ga 3 ta kichik o'zgarish bilan ulanadi):

✨ Premium (custom) emojilar
   /addemoji <premium emoji(lar)>  — (admin) premium emojini botga o'rgatish
   /delemoji <emoji>               — (admin) o'chirish
   /emojis                         — (admin) o'rgatilganlar ro'yxati
   /emojiid                        — premium emojining ID sini ko'rsatadi
   /addpack <havola>               — (admin) butun premium emoji paketini birdan o'rgatish
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
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters, ApplicationHandlerStop

import admin_store
import ai_core
import db
import game_store
import userbot_store

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
            raw = dict(db.load(_EMOJI_STORE, "map", {}))
            # eski format (matn) -> ro'yxat
            _emoji_cache = {k: (v if isinstance(v, list) else [v]) for k, v in raw.items() if v}
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


SAVE_WARN = "\n\n⚠️ Bazaga saqlab bo'lmadi (DATABASE_URL yo'q yoki xato) — bot qayta ishga tushsa yo'qoladi."


def _save_emoji_map() -> bool:
    _rebuild_re()
    try:
        db.save(_EMOJI_STORE, "map", _emoji_cache or {})
        return True
    except Exception:
        logger.exception("premium emoji xaritasini saqlab bo'lmadi")
        return False


def emoji_id(ch: str) -> str | None:
    ids = _emoji_map().get(_norm(ch))
    return random.choice(ids) if ids else None


MAX_VARIANTS_PER_EMOJI = 8


def _add_emoji_id(ch: str, eid: str) -> bool:
    _emoji_map()
    ids = _emoji_cache.setdefault(_norm(ch), [])
    if eid in ids:
        return False
    if len(ids) >= MAX_VARIANTS_PER_EMOJI:
        return False
    ids.append(eid)
    return True


def pe(ch: str) -> str:
    """HTML ichida ishlatiladigan emoji: o'rgatilgan bo'lsa premium, bo'lmasa oddiy."""
    eid = emoji_id(ch)
    if eid:
        return f'<tg-emoji emoji-id="{eid}">{ch}</tg-emoji>'
    return ch


def strip_premium(text: str) -> str:
    """<tg-emoji> teglarini oddiy emojiga qaytaradi (fallback uchun)."""
    return _TG_EMOJI_RE.sub(r"\1", text)


def _sub_emojis(s: str) -> tuple[str, int]:
    """Tayyor (HTML-xavfsiz) matndagi o'rgatilgan emojilarni <tg-emoji> ga almashtiradi."""
    count = 0

    def _sub(m: re.Match) -> str:
        nonlocal count
        ids = _emoji_cache.get(m.group(1))
        if not ids or count >= MAX_PREMIUM_PER_MESSAGE:
            return m.group(0)
        count += 1
        return f'<tg-emoji emoji-id="{random.choice(ids)}">{m.group(1)}</tg-emoji>'

    return _emoji_re.sub(_sub, s), count


def premiumize(text: str) -> str | None:
    """Oddiy matndagi emojilarni premium'ga almashtiradi va HTML qaytaradi.
    O'rgatilgan emoji yo'q yoki matnda uchramasa — None (oddiy yuboriladi)."""
    _emoji_map()
    if not _emoji_re or not text:
        return None
    out, count = _sub_emojis(html.escape(text, quote=False))
    return out if count else None


_HTML_TAG_RE = re.compile(r"(<[^>]*>)")


def premiumize_html(text: str) -> str | None:
    """Allaqachon HTML bo'lgan matn uchun: teglar ichiga tegmasdan emojilarni almashtiradi."""
    _emoji_map()
    if not _emoji_re or not text or "<tg-emoji" in text:
        return None
    total = 0
    parts = []
    for chunk in _HTML_TAG_RE.split(text):
        if chunk.startswith("<") and chunk.endswith(">"):
            parts.append(chunk)
        else:
            c, n = _sub_emojis(chunk)
            parts.append(c)
            total += n
    return "".join(parts) if total else None


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

    if PLAIN_EMOJI_LIMIT:
        text = drop_plain_emojis(text)
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
        if _add_emoji_id(ch, ent.custom_emoji_id):
            added.append(ch)
    saved = _save_emoji_map()
    await msg.reply_text(
        f"✅ {len(added)} ta premium emoji o'rgatildi: {' '.join(added) or '(allaqachon bor edi)'}\n"
        "Endi botdagi shu oddiy emojilar o'rniga premium variant chiqadi."
        + ("" if saved else SAVE_WARN)
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
    saved = _save_emoji_map() if removed else True
    await msg.reply_text(
        (f"🗑 {removed} ta o'chirildi." if removed else "Bunday emoji o'rgatilmagan.") + ("" if saved else SAVE_WARN)
    )


async def cmd_emojis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return
    m = _emoji_map()
    if not m:
        await msg.reply_text("Hali premium emoji o'rgatilmagan. /addemoji dan foydalan.")
        return
    total_ids = sum(len(v) for v in m.values())
    keys = list(m.keys())[:60]
    await reply_html(
        msg,
        f"Premium o'rgatilgan: {len(m)} xil emoji, jami {total_ids} ta variant\n"
        + " ".join(pe(k) for k in keys)
        + f"\n\nOddiy ko'rinishi: {_esc(' '.join(keys))}",
    )


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

# ═══════════════════════════════════════════════════════════════════════════
# 🌐 TIL (uz / ru / en)
# ═══════════════════════════════════════════════════════════════════════════

_LANGS = {"uz": "🇺🇿 O'zbekcha", "ru": "🇷🇺 Русский", "en": "🇬🇧 English"}
_LANG_STORE = "user_lang"
_lang_cache: dict[str, str] = {}


def _stored_lang(user_id) -> str | None:
    k = str(user_id)
    if k in _lang_cache:
        return _lang_cache[k]
    try:
        v = db.load(_LANG_STORE, k, None)
    except Exception:
        v = None
    if v in _LANGS:
        _lang_cache[k] = v
        return v
    return None


def _get_lang(user_id) -> str:
    return _stored_lang(user_id) or "uz"


def _set_lang(user_id, lang: str) -> None:
    _lang_cache[str(user_id)] = lang
    try:
        db.save(_LANG_STORE, str(user_id), lang)
    except Exception:
        logger.exception("tilni saqlab bo'lmadi")


def _ensure_lang(user) -> str:
    """Birinchi /start da Telegram tilidan taxmin qilamiz (keyin o'zi o'zgartira oladi)."""
    if not user:
        return "uz"
    cur = _stored_lang(user.id)
    if cur:
        return cur
    code = (getattr(user, "language_code", "") or "").lower()[:2]
    lang = code if code in ("ru", "en") else "uz"
    _set_lang(user.id, lang)
    return lang


def _lang_hint(user_id) -> str:
    lang = _get_lang(user_id) if user_id is not None else "uz"
    if lang == "ru":
        return "TIL: foydalanuvchi bilan RUS tilida gaplash va javoblarni ruscha yoz (foydalanuvchi boshqa tilda yozsa, o'sha tilda javob ber)."
    if lang == "en":
        return "TIL: foydalanuvchi bilan INGLIZ tilida gaplash va javoblarni inglizcha yoz (foydalanuvchi boshqa tilda yozsa, o'sha tilda javob ber)."
    return ""


# ═══════════════════════════════════════════════════════════════════════════
# 🚀 /start — bosh sahifa -> bo'lim -> batafsil (3 daraja, uch tilda)
# ═══════════════════════════════════════════════════════════════════════════

T = {
    "uz": {
        "hello_title": "Salom, {name}!",
        "hello_body": "Men <b>{bot}</b>man: gaplashaman, o'yin o'ynataman, rasm chizaman va ovozda gapiraman.\n\nQuyidan bo'lim tanla yoki shunchaki yoz.",
        "open": "{bot} ni ochish", "add_group": "Guruhga qo'shish", "back": "Orqaga", "home": "Bosh sahifa",
        "sec_ai": "AI imkoniyatlari", "sec_play": "O'yin va fun", "sec_tools": "Foydali", "sec_account": "Hisob",
        "desc_ai": "Suhbat, rasm, ovoz, YouTube, anime", "desc_play": "O'yinlar, ship va qiziqarli buyruqlar",
        "desc_tools": "Tarjima, ob-havo, eslatma va boshqalar", "pick": "Batafsil ko'rish uchun tanla:",
        "acc_title": "Hisobim", "acc_lang": "Til", "acc_msgs": "Xabarlar", "acc_games": "O'yinlar",
        "acc_wins": "g'alaba", "acc_losses": "mag'lubiyat", "acc_draws": "durang",
        "acc_link_yes": "Akkount: ulangan", "acc_link_no": "Akkount: ulanmagan",
        "acc_pro": "Holat: Misumi AI Pro", "acc_free": "Holat: oddiy",
        "why_link": "Shaxsiy akkountingni ulasang, sen oflayn bo'lganingda AI sening o'rningga tabiiy javob yozadi va bio'ingni yangilab turadi.",
        "btn_link": "Akkount ulash", "btn_services": "Xizmatlar", "btn_stats": "Statistika", "btn_disc": "Akkountni uzish",
        "btn_pro": "Pro sotib olish", "btn_lang": "Til", "btn_voice": "Ovoz", "btn_clear": "Suhbatni tozalash",
        "lang_title": "Tilni tanla. Menyular va AI javoblari shu tilda bo'ladi:", "lang_set": "Til o'zgartirildi",
        "voice_title": "Qaysi ovozda gapiray?", "clear_ask": "Suhbat tarixini tozalaymi? Bu amalni qaytarib bo'lmaydi.",
        "clear_yes": "Ha, tozala", "clear_no": "Bekor qilish", "clear_done": "Suhbat tozalandi",
    },
    "ru": {
        "hello_title": "Привет, {name}!",
        "hello_body": "Я <b>{bot}</b>: болтаю, играю, рисую и отвечаю голосом.\n\nВыбери раздел ниже или просто напиши.",
        "open": "Открыть {bot}", "add_group": "Добавить в группу", "back": "Назад", "home": "Главная",
        "sec_ai": "Возможности ИИ", "sec_play": "Игры и фан", "sec_tools": "Полезное", "sec_account": "Аккаунт",
        "desc_ai": "Чат, рисунки, голос, YouTube, аниме", "desc_play": "Игры, шип и весёлые команды",
        "desc_tools": "Перевод, погода, напоминания и другое", "pick": "Выбери, чтобы узнать подробнее:",
        "acc_title": "Мой аккаунт", "acc_lang": "Язык", "acc_msgs": "Сообщений", "acc_games": "Игры",
        "acc_wins": "побед", "acc_losses": "поражений", "acc_draws": "ничьих",
        "acc_link_yes": "Аккаунт: подключён", "acc_link_no": "Аккаунт: не подключён",
        "acc_pro": "Статус: Misumi AI Pro", "acc_free": "Статус: обычный",
        "why_link": "Подключи личный аккаунт, и пока тебя нет онлайн, ИИ естественно ответит за тебя и будет обновлять твоё био.",
        "btn_link": "Подключить аккаунт", "btn_services": "Сервисы", "btn_stats": "Статистика", "btn_disc": "Отключить аккаунт",
        "btn_pro": "Купить Pro", "btn_lang": "Язык", "btn_voice": "Голос", "btn_clear": "Очистить диалог",
        "lang_title": "Выбери язык. Меню и ответы ИИ будут на нём:", "lang_set": "Язык изменён",
        "voice_title": "Каким голосом говорить?", "clear_ask": "Очистить историю диалога? Это нельзя отменить.",
        "clear_yes": "Да, очистить", "clear_no": "Отмена", "clear_done": "Диалог очищен",
    },
    "en": {
        "hello_title": "Hi, {name}!",
        "hello_body": "I'm <b>{bot}</b>: I chat, play games, draw pictures and talk by voice.\n\nPick a section below or just type.",
        "open": "Open {bot}", "add_group": "Add to group", "back": "Back", "home": "Home",
        "sec_ai": "AI features", "sec_play": "Games & fun", "sec_tools": "Tools", "sec_account": "Account",
        "desc_ai": "Chat, images, voice, YouTube, anime", "desc_play": "Games, ship and fun commands",
        "desc_tools": "Translate, weather, reminders and more", "pick": "Pick one for details:",
        "acc_title": "My account", "acc_lang": "Language", "acc_msgs": "Messages", "acc_games": "Games",
        "acc_wins": "wins", "acc_losses": "losses", "acc_draws": "draws",
        "acc_link_yes": "Account: linked", "acc_link_no": "Account: not linked",
        "acc_pro": "Status: Misumi AI Pro", "acc_free": "Status: free",
        "why_link": "Link your personal account and while you're offline the AI will reply for you naturally and keep your bio up to date.",
        "btn_link": "Link account", "btn_services": "Services", "btn_stats": "Stats", "btn_disc": "Unlink account",
        "btn_pro": "Get Pro", "btn_lang": "Language", "btn_voice": "Voice", "btn_clear": "Clear chat",
        "lang_title": "Pick a language. Menus and AI replies will use it:", "lang_set": "Language changed",
        "voice_title": "Which voice should I use?", "clear_ask": "Clear the chat history? This can't be undone.",
        "clear_yes": "Yes, clear", "clear_no": "Cancel", "clear_done": "Chat cleared",
    },
}


def tr(lang: str, key: str, **kw) -> str:
    s = T.get(lang, T["uz"]).get(key) or T["uz"][key]
    return s.format(**kw) if kw else s


# bo'lim -> (emoji, [elementlar])
_SECTIONS = {
    "ai": ("🤖", ["chat", "imagine", "voice", "photo", "yt", "anime"]),
    "play": ("🎮", ["duel", "ship", "fun"]),
    "tools": ("🛠", ["util", "group", "sticker"]),
}

# element -> (emoji, (uz, ru, en) nom, (uz, ru, en) matn)
_ITEMS = {
    "chat": ("💬", ("Suhbat", "Чат", "Chat"), (
        "Oddiy yoz, javob beraman.\nGuruhda «Misumi» deb chaqir yoki xabarimga reply qil.\n/reset — suhbatni tozalash",
        "Просто пиши, я отвечу.\nВ группе позови «Misumi» или ответь на моё сообщение.\n/reset — очистить диалог",
        "Just write and I'll answer.\nIn groups say «Misumi» or reply to my message.\n/reset — clear the chat")),
    "imagine": ("🎨", ("Rasm chizish", "Рисунки", "Images"), (
        "/imagine tavsif — rasm chizaman.\nMasalan: /imagine kechki Toshkent, anime uslubida\nKuniga 15 tagacha.",
        "/imagine описание — нарисую картинку.\nНапример: /imagine вечерний Ташкент в стиле аниме\nДо 15 в день.",
        "/imagine description — I'll draw it.\nExample: /imagine Tashkent at night, anime style\nUp to 15 per day.")),
    "voice": ("🎙", ("Ovoz", "Голос", "Voice"), (
        "Ovozli xabar yubor — ovozda javob beraman.\n/ovoz matn — matnni ovozga aylantirish\n/ovozsoz — ovoz turini tanlash (Hisob bo'limida ham bor)",
        "Отправь голосовое — отвечу голосом.\n/ovoz текст — озвучить текст\n/ovozsoz — выбрать голос (есть и в разделе «Аккаунт»)",
        "Send a voice message and I'll answer by voice.\n/ovoz text — read text aloud\n/ovozsoz — pick a voice (also in Account)")),
    "photo": ("🖼", ("Rasm tahlili", "Анализ фото", "Photo analysis"), (
        "Rasm yubor, tugmalar chiqadi:\n• Tushuntir\n• Roast\n• Qaysi anime personaj?\n• Matnni o'qi\nSarlavha yozib yuborsang, savolingga javob beraman.",
        "Отправь фото, появятся кнопки:\n• Объяснить\n• Roast\n• На какого аниме-персонажа похож?\n• Прочитать текст\nЕсли добавишь подпись, отвечу на твой вопрос.",
        "Send a photo and buttons appear:\n• Explain\n• Roast\n• Which anime character?\n• Read the text\nAdd a caption and I'll answer your question.")),
    "yt": ("▶️", ("YouTube", "YouTube", "YouTube"), (
        "/yt nom yoki havola — video topish\n/ytber nom — YouTuber haqida\nYouTube havolasini tashlasang ham bo'ladi yoki «... videosini top» de.",
        "/yt название или ссылка — найти видео\n/ytber имя — о ютубере\nМожно просто скинуть ссылку или написать «найди видео ...».",
        "/yt name or link — find a video\n/ytber name — about a YouTuber\nYou can also just drop a link or say «find a video of ...».")),
    "anime": ("🎬", ("Anime", "Аниме", "Anime"), (
        "/anime nom — reyting, janr, qismlar, mazmun\nNomsiz /anime — tasodifiy tavsiya.",
        "/anime название — рейтинг, жанр, серии, сюжет\n/anime без названия — случайная рекомендация.",
        "/anime name — rating, genres, episodes, synopsis\n/anime alone — a random pick.")),
    "duel": ("⚔️", ("O'yinlar", "Игры", "Games"), (
        "/duel — o'yin taklif qilish\n/battle — jang\n/tictactoe — X-O\n/rps — tosh-qaychi-qog'oz\n/quiz — viktorina\n/reyting — reyting\nGuruhda eng qiziq.",
        "/duel — вызвать на игру\n/battle — бой\n/tictactoe — крестики-нолики\n/rps — камень-ножницы-бумага\n/quiz — викторина\n/reyting — рейтинг\nВ группе веселее всего.",
        "/duel — challenge someone\n/battle — fight\n/tictactoe — X-O\n/rps — rock-paper-scissors\n/quiz — trivia\n/reyting — leaderboard\nBest in a group.")),
    "ship": ("💞", ("Ship", "Шип", "Ship"), (
        "/ship — tasodifiy juftlik\n/shipleader · /shipfact · /shipmemory\n/marry · /couple · /soulmate · /crush · /dating\n/love · /exship · /divorced\n/juftim — juft kartasi · /ajrashish — nikohni bekor qilish\n/shiptag on|off — guruh teglari (admin)",
        "/ship — случайная пара\n/shipleader · /shipfact · /shipmemory\n/marry · /couple · /soulmate · /crush · /dating\n/love · /exship · /divorced\n/juftim — карточка пары · /ajrashish — развод\n/shiptag on|off — теги в группе (админ)",
        "/ship — a random couple\n/shipleader · /shipfact · /shipmemory\n/marry · /couple · /soulmate · /crush · /dating\n/love · /exship · /divorced\n/juftim — couple card · /ajrashish — divorce\n/shiptag on|off — group tags (admin)")),
    "fun": ("🎲", ("Fun", "Фан", "Fun"), (
        "/roast · /compliment · /rate · /expose\n/8ball · /horoscope · /lucky · /quote\n/truth · /dare · /wouldyourather\n/confession · /ngl · /fakechat",
        "/roast · /compliment · /rate · /expose\n/8ball · /horoscope · /lucky · /quote\n/truth · /dare · /wouldyourather\n/confession · /ngl · /fakechat",
        "/roast · /compliment · /rate · /expose\n/8ball · /horoscope · /lucky · /quote\n/truth · /dare · /wouldyourather\n/confession · /ngl · /fakechat")),
    "util": ("🧰", ("Asboblar", "Инструменты", "Utilities"), (
        "/translate — tarjima\n/weather — ob-havo\n/calc — kalkulyator\n/remind — eslatma",
        "/translate — перевод\n/weather — погода\n/calc — калькулятор\n/remind — напоминание",
        "/translate — translation\n/weather — weather\n/calc — calculator\n/remind — reminder")),
    "group": ("📊", ("Guruh", "Группа", "Group"), (
        "/stats · /who · /activity · /members — guruh statistikasi va a'zolar",
        "/stats · /who · /activity · /members — статистика группы и участники",
        "/stats · /who · /activity · /members — group stats and members")),
    "sticker": ("🌟", ("Stikerlar", "Стикеры", "Stickers"), (
        "/stikeryasa — rasmdan yoki tavsifdan stiker yasash (fonsiz, WEBP)\n/stikerpaket — shaxsiy paketing\n/stiker — stikerni turkumga qo'shish\n/stikerlar — turkumlar ro'yxati",
        "/stikeryasa — стикер из фото или описания (без фона, WEBP)\n/stikerpaket — твой пак\n/stiker — добавить стикер в категорию\n/stikerlar — список категорий",
        "/stikeryasa — make a sticker from a photo or text (no bg, WEBP)\n/stikerpaket — your pack\n/stiker — add a sticker to a category\n/stikerlar — list categories")),
}
_LI = {"uz": 0, "ru": 1, "en": 2}


def _btn_maker(premium: bool):
    return icon_btn if premium else plain_btn


def _start_text(first_name: str | None, lang: str = "uz") -> str:
    name = _esc(first_name) if first_name else ("do'st" if lang == "uz" else "друг" if lang == "ru" else "friend")
    return f"{pe('✨')} <b>{tr(lang, 'hello_title', name=name)}</b>\n" + tr(lang, "hello_body", bot=_esc(ai_core.BOT_NAME))


def _start_kb(chat_id, bot_username: str | None, premium: bool, lang: str = "uz") -> InlineKeyboardMarkup:
    mk = _btn_maker(premium)
    rows = []
    if WEBAPP_URL:
        rows.append([mk(tr(lang, "open", bot=ai_core.BOT_NAME), "✨", web_app=WebAppInfo(url=WEBAPP_URL))])
    rows.append([
        mk(tr(lang, "sec_ai"), _SECTIONS["ai"][0], callback_data="st:s:ai"),
        mk(tr(lang, "sec_play"), _SECTIONS["play"][0], callback_data="st:s:play"),
    ])
    rows.append([
        mk(tr(lang, "sec_tools"), _SECTIONS["tools"][0], callback_data="st:s:tools"),
        mk(tr(lang, "sec_account"), "👤", callback_data="ac:home"),
    ])
    if bot_username:
        rows.append([mk(tr(lang, "add_group"), "➕", url=f"https://t.me/{bot_username}?startgroup=true")])
    if WEBAPP_URL and _is_admin(chat_id):
        rows.append([mk("Admin", "⚙️", web_app=WebAppInfo(url=f"{WEBAPP_URL.rstrip('/')}/admin"))])
    return InlineKeyboardMarkup(rows)


async def send_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user
    lang = _ensure_lang(user)
    text = _start_text(user.first_name if user else None, lang)
    bot_username = context.bot.username
    await reply_html(
        update.effective_message, text,
        kb_factory=lambda prem: _start_kb(chat_id, bot_username, prem, lang),
    )


def _section_text(sec: str, lang: str) -> str:
    emoji = _SECTIONS[sec][0]
    return f"{pe(emoji)} <b>{tr(lang, 'sec_' + sec)}</b>\n{tr(lang, 'desc_' + sec)}\n\n{tr(lang, 'pick')}"


def _section_kb(sec: str, lang: str, premium: bool) -> InlineKeyboardMarkup:
    mk = _btn_maker(premium)
    li = _LI[lang]
    btns = [mk(_ITEMS[k][1][li], _ITEMS[k][0], callback_data=f"st:i:{sec}:{k}") for k in _SECTIONS[sec][1]]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([mk(tr(lang, "back"), "⬅️", callback_data="st:home")])
    return InlineKeyboardMarkup(rows)


def _item_text(item: str, lang: str) -> str:
    emoji, names, bodies = _ITEMS[item]
    li = _LI[lang]
    return f"{pe(emoji)} <b>{_esc(names[li])}</b>\n\n<blockquote>{_esc(bodies[li])}</blockquote>"


def _item_kb(sec: str, lang: str, premium: bool) -> InlineKeyboardMarkup:
    mk = _btn_maker(premium)
    return InlineKeyboardMarkup([[
        mk(tr(lang, "back"), "⬅️", callback_data=f"st:s:{sec}"),
        mk(tr(lang, "home"), "🏠", callback_data="st:home"),
    ]])


async def start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    user = query.from_user
    lang = _get_lang(user.id) if user else "uz"
    chat_id = query.message.chat.id if query.message else update.effective_chat.id
    parts = data.split(":")
    if data == "st:home" or data in ("st:cmds",) or data.startswith("st:p:"):
        bu = context.bot.username
        text = _start_text(user.first_name if user else None, lang)
        await edit_html(query, text, lambda prem: _start_kb(chat_id, bu, prem, lang))
    elif len(parts) == 3 and parts[1] == "s" and parts[2] in _SECTIONS:
        sec = parts[2]
        await edit_html(query, _section_text(sec, lang), lambda prem: _section_kb(sec, lang, prem))
    elif len(parts) == 4 and parts[1] == "i" and parts[2] in _SECTIONS and parts[3] in _ITEMS:
        sec, item = parts[2], parts[3]
        await edit_html(query, _item_text(item, lang), lambda prem: _item_kb(sec, lang, prem))


# ═══════════════════════════════════════════════════════════════════════════
# 👤 HISOB — profil, akkount ulash, til, ovoz, suhbatni tozalash
# ═══════════════════════════════════════════════════════════════════════════

def _mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return f"+{digits[:3]}•••{digits[-2:]}" if len(digits) > 6 else "•••"


def _account_info(user, lang: str) -> tuple[str, bool]:
    """(HTML matn, akkount ulanganmi)"""
    uid = user.id
    lines = [f"{pe('👤')} <b>{tr(lang, 'acc_title')}</b>", f"{_esc(user.first_name or '')} · <code>{uid}</code>"]
    lines.append(f"{pe('🌐')} {tr(lang, 'acc_lang')}: {_LANGS[lang]}")
    try:
        entry = (admin_store._load().get("users") or {}).get(str(uid)) or {}
    except Exception:
        entry = {}
    if entry.get("message_count"):
        since = (entry.get("first_seen") or "")[:10]
        lines.append(f"{pe('💬')} {tr(lang, 'acc_msgs')}: {entry['message_count']}" + (f" · {pe('📅')} {since}" if since else ""))
    try:
        g = game_store.get_stats(uid) or {}
    except Exception:
        g = {}
    if any(g.get(k) for k in ("wins", "losses", "draws")):
        lines.append(
            f"{pe('🎮')} {tr(lang, 'acc_games')}: {g.get('wins', 0)} {tr(lang, 'acc_wins')} · "
            f"{g.get('losses', 0)} {tr(lang, 'acc_losses')} · {g.get('draws', 0)} {tr(lang, 'acc_draws')}"
        )
    try:
        connected = bool(userbot_store.is_connected(uid))
        meta = (userbot_store.get_account_meta(uid) or {}) if connected else {}
        pro = bool(admin_store.is_premium(uid))
    except Exception:
        connected, meta, pro = False, {}, False
    if connected:
        lines.append(f"{pe('🔗')} {tr(lang, 'acc_link_yes')} ({_esc(_mask_phone(meta.get('phone', '')))})")
    else:
        lines.append(f"{pe('🔗')} {tr(lang, 'acc_link_no')}")
    lines.append(f"{pe('💎')} {tr(lang, 'acc_pro') if pro else tr(lang, 'acc_free')}")
    text = "\n".join(lines)
    if not connected:
        text += f"\n\n<blockquote>{_esc(tr(lang, 'why_link'))}</blockquote>"
    return text, connected


def _account_kb(connected: bool, pro: bool, lang: str, premium: bool) -> InlineKeyboardMarkup:
    mk = _btn_maker(premium)
    rows = []
    if connected:
        rows.append([mk(tr(lang, "btn_services"), "⚙️", callback_data="ub:services"),
                     mk(tr(lang, "btn_stats"), "📊", callback_data="ub:stats")])
    else:
        rows.append([mk(tr(lang, "btn_link"), "🔗", callback_data="ub:connect")])
    if not pro:
        rows.append([mk(tr(lang, "btn_pro"), "💎", callback_data="ub:buy_pro")])
    rows.append([mk(tr(lang, "btn_lang"), "🌐", callback_data="ac:lang"),
                 mk(tr(lang, "btn_voice"), "🔊", callback_data="ac:voice")])
    rows.append([mk(tr(lang, "btn_clear"), "🧹", callback_data="ac:clear")])
    if connected:
        rows.append([mk(tr(lang, "btn_disc"), "🔌", callback_data="ub:disconnect")])
    rows.append([mk(tr(lang, "back"), "⬅️", callback_data="st:home")])
    return InlineKeyboardMarkup(rows)


async def _show_account(query, lang: str) -> None:
    user = query.from_user
    text, connected = _account_info(user, lang)
    try:
        pro = bool(admin_store.is_premium(user.id))
    except Exception:
        pro = False
    await edit_html(query, text, lambda prem: _account_kb(connected, pro, lang, prem))


def _lang_kb(current: str, lang: str, premium: bool) -> InlineKeyboardMarkup:
    mk = _btn_maker(premium)
    rows = [[InlineKeyboardButton(("✅ " if k == current else "") + label, callback_data=f"ac:setlang:{k}")]
            for k, label in _LANGS.items()]
    rows.append([mk(tr(lang, "back"), "⬅️", callback_data="ac:home")])
    return InlineKeyboardMarkup(rows)


def _voice_kb(current: str, lang: str, premium: bool) -> InlineKeyboardMarkup:
    mk = _btn_maker(premium)
    rows = [[InlineKeyboardButton(("✅ " if k == current else "") + label, callback_data=f"ac:setvoice:{k}")]
            for k, (_v, label) in VOICES.items()]
    rows.append([mk(tr(lang, "back"), "⬅️", callback_data="ac:home")])
    return InlineKeyboardMarkup(rows)


async def account_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    user = query.from_user
    lang = _get_lang(user.id)
    parts = data.split(":")

    if data in ("ac:home", "ub:account"):
        await query.answer()
        await _show_account(query, lang)
    elif data == "ac:lang":
        await query.answer()
        await edit_html(query, f"{pe('🌐')} {tr(lang, 'lang_title')}", lambda prem: _lang_kb(lang, lang, prem))
    elif len(parts) == 3 and parts[1] == "setlang" and parts[2] in _LANGS:
        _set_lang(user.id, parts[2])
        lang = parts[2]
        await query.answer(tr(lang, "lang_set"))
        await _show_account(query, lang)
    elif data == "ac:voice":
        await query.answer()
        await edit_html(query, f"{pe('🔊')} {tr(lang, 'voice_title')}",
                        lambda prem: _voice_kb(_get_voice_key(user.id), lang, prem))
    elif len(parts) == 3 and parts[1] == "setvoice" and parts[2] in VOICES:
        _set_voice_key(user.id, parts[2])
        await query.answer("✅")
        await edit_html(query, f"{pe('🔊')} {tr(lang, 'voice_title')}",
                        lambda prem: _voice_kb(parts[2], lang, prem))
    elif data == "ac:clear":
        await query.answer()
        mk_rows = lambda prem: InlineKeyboardMarkup([[
            _btn_maker(prem)(tr(lang, "clear_yes"), "🧹", callback_data="ac:clear:yes"),
            _btn_maker(prem)(tr(lang, "clear_no"), "✖️", callback_data="ac:home"),
        ]])
        await edit_html(query, f"{pe('🧹')} {tr(lang, 'clear_ask')}", mk_rows)
    elif data == "ac:clear:yes":
        try:
            ai_core.reset_user(query.message.chat.id)
        except Exception:
            logger.exception("suhbatni tozalab bo'lmadi")
        await query.answer(tr(lang, "clear_done"), show_alert=False)
        await _show_account(query, lang)
    else:
        await query.answer()
    if data == "ub:account":
        raise ApplicationHandlerStop         # eski oddiy menyu o'rniga yangi Hisob bo'limi


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
        k = db.load(_TTS_STORE, str(user_id), None)
    except Exception:
        k = None
    if k in VOICES:
        return k
    return {"ru": "ru", "en": "en"}.get(_get_lang(user_id), "uz_f")     # tanlanmagan bo'lsa — til bo'yicha


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
    sticker_btn = mk("Stiker yasash", "🧩", callback_data=f"sk:photo:{key}")
    return InlineKeyboardMarkup([items[:2], items[2:], [sticker_btn]])


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
# 💎 PREMIUM EMOJI'NI KUCHAYTIRISH
#    • /addpack — butun premium emoji paketini birdan o'rgatish
#    • AI javoblarida emojilar ko'proq ishlatiladi
#    • Botning hamma xabaridagi oddiy emoji avtomatik premium'ga almashadi
#    • Tugmalardagi oddiy emoji ham premium ikonkaga almashadi
#    • AI javobidagi o'rgatilmagan oddiy emojilar olib tashlanadi (premium ko'p, oddiy kam)
#    • Stikerlar bazadan avvalgidek ishlaydi (xohlasangiz PREMIUM_INSTEAD_OF_STICKER bilan almashtirish mumkin)
# ═══════════════════════════════════════════════════════════════════════════

PREMIUM_INSTEAD_OF_STICKER = float(os.environ.get("PREMIUM_INSTEAD_OF_STICKER", "0"))  # 0..1, 0 = stikerlar avvalgidek
PLAIN_EMOJI_LIMIT = os.environ.get("PLAIN_EMOJI_LIMIT", "1") != "0"   # AI javobida o'rgatilmagan oddiy emojilarni olib tashlash
PREMIUM_EMOJI_GLOBAL = os.environ.get("PREMIUM_EMOJI_GLOBAL", "1") != "0"
_STICKER_MARK = "__premium_mood__:"

# stiker turkumi -> mos emojilar (shulardan o'rgatilgani tanlanadi)
_MOOD_POOLS = {
    "happy": ["😊", "😄", "😁", "🥰", "✨", "🎉", "😃"],
    "laugh": ["😂", "🤣", "😹", "😆", "😅"],
    "love": ["❤", "😍", "🥰", "💕", "😘", "💖", "💘"],
    "sad": ["😢", "😭", "🥺", "💔", "😔"],
    "angry": ["😡", "🤬", "😤", "😠", "👿"],
    "surprised": ["😮", "😲", "🤯", "😱", "😳"],
    "cool": ["😎", "🔥", "💪", "👌", "😏"],
    "thinking": ["🤔", "🧐", "🙄"],
    "general": ["👍", "🙂", "✨", "😉", "🙌"],
    "win": ["🏆", "🎉", "🥇", "🔥", "💪", "😎"],
    "lose": ["😭", "😢", "💀", "🤡", "😵"],
    "draw": ["🤝", "😐", "🙃", "😅"],
}


def _mood_pick(category: str) -> str | None:
    """Turkumga mos, o'rgatilgan premium emojilardan 1-3 tasini tanlab, HTML qaytaradi."""
    m = _emoji_map()
    have = [e for e in _MOOD_POOLS.get(category, []) if _norm(e) in m]
    if not have:
        return None
    k = min(len(have), random.choice((1, 1, 2, 3)))
    chosen = random.sample(have, k)
    return "".join(pe(e) for e in chosen)


def emoji_style_hint() -> str:
    """AI ga: emojilarni ko'proq va faqat o'rgatilganlardan ishlat."""
    m = _emoji_map()
    if not m:
        return ""
    chars = list(m.keys())
    random.shuffle(chars)
    return (
        "EMOJI USLUBI: javobingga kayfiyatga mos emojilarni tabiiy joylarda ko'proq qo'sh "
        "(har gapda yoki har 1-2 gapda 1-3 ta). FAQAT shu ro'yxatdagi emojilardan foydalan: "
        + " ".join(chars[:40])
        + ". Ro'yxatda yo'q emojini hech qachon yozma, mos kelmasa emojisiz yoz. "
        "Stiker tegi ([STIKER...]) ishlatish mumkin, lekin oddiy emojilarni kam ishlat."
    )


def drop_plain_emojis(text: str) -> str:
    """AI javobidan o'rgatilmagan (oddiy) emojilarni olib tashlaydi: premium ko'p, oddiy kam."""
    _emoji_map()
    if not _emoji_re or not text:
        return text

    def fix(m: re.Match) -> str:
        parts = _emoji_re.split(m.group(0))      # toq indekslar — o'rgatilgan emojilar
        return "".join(p for i, p in enumerate(parts) if i % 2 == 1)

    out = _EMOJI_CLEAN_RE.sub(fix, text)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r" +([,.!?])", r"\1", out)
    out = out.strip()
    return out or text


async def cmd_addpack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return
    arg = (context.args[0] if context.args else "").strip()
    m = re.search(r"(?:addemoji|addstickers)/([A-Za-z0-9_]+)", arg)
    name = m.group(1) if m else (arg if re.fullmatch(r"[A-Za-z0-9_]{3,64}", arg) else "")
    if not name:
        await msg.reply_text(
            "Premium emoji paketining havolasini yoz:\n"
            "/addpack https://t.me/addemoji/PaketNomi\n\n"
            "Paket havolasini premium emojini bosib, \"Paketni ko'rish\" > \"Ulashish\" orqali olasan."
        )
        return
    try:
        pack = await context.bot.get_sticker_set(name)
    except TelegramError:
        await msg.reply_text("paket topilmadi. havolani tekshir.")
        return
    stickers = [s for s in pack.stickers if getattr(s, "custom_emoji_id", None) and s.emoji]
    if not stickers:
        await msg.reply_text("bu premium emoji paketi emas (oddiy stiker paketi). /addemoji bilan alohida qo'sh.")
        return
    added = 0
    kinds = set()
    for st in stickers:
        if _add_emoji_id(st.emoji, st.custom_emoji_id):
            added += 1
        kinds.add(_norm(st.emoji))
    saved = _save_emoji_map()
    await msg.reply_text(
        f"✅ «{pack.title}» paketidan {added} ta premium emoji qo'shildi ({len(kinds)} xil emoji).\n"
        "Bir emojiga bir nechta variant bo'lsa, bot tasodifiy birini tanlaydi. Ro'yxat: /emojis"
        + ("\n💾 Bazaga saqlandi, bot qayta ishga tushsa ham yo'qolmaydi." if saved else SAVE_WARN)
    )


def _iconify(b: InlineKeyboardButton) -> InlineKeyboardButton | None:
    """Tugma matni boshidagi o'rgatilgan emojini premium ikonkaga aylantiradi."""
    if getattr(b, "icon_custom_emoji_id", None) or not _emoji_re:
        return None
    t = b.text or ""
    m = _emoji_re.match(t)
    if not m:
        return None
    ids = _emoji_cache.get(m.group(1))
    rest = t[m.end():].lstrip()
    if not ids or not rest:
        return None
    try:
        d = b.to_dict()
        d["text"] = rest
        d["icon_custom_emoji_id"] = random.choice(ids)
        return InlineKeyboardButton.de_json(d)
    except Exception:
        return None


def _premium_markup(mk: InlineKeyboardMarkup) -> InlineKeyboardMarkup | None:
    _emoji_map()
    if not _emoji_re:
        return None
    changed = False
    rows = []
    for row in mk.inline_keyboard:
        new_row = []
        for b in row:
            nb = _iconify(b)
            changed = changed or nb is not None
            new_row.append(nb or b)
        rows.append(new_row)
    return InlineKeyboardMarkup(rows) if changed else None


def _premium_kwargs(kwargs: dict, key: str = "text") -> dict | None:
    """send_message/send_photo/... argumentlarini premium variantga o'giradi. Kerak bo'lmasa None."""
    new = dict(kwargs)
    changed = False
    text = kwargs.get(key)
    ents = kwargs.get("entities") or kwargs.get("caption_entities")
    if isinstance(text, str) and not ents:
        pm = kwargs.get("parse_mode")
        pm_val = getattr(pm, "value", pm)
        rich = None
        if pm_val is None:
            rich = premiumize(text)
        elif str(pm_val).upper() == "HTML":
            rich = premiumize_html(text)
        if rich:
            new[key] = rich
            new["parse_mode"] = "HTML"
            changed = True
    mk = kwargs.get("reply_markup")
    if isinstance(mk, InlineKeyboardMarkup):
        m2 = _premium_markup(mk)
        if m2 is not None:
            new["reply_markup"] = m2
            changed = True
    return new if changed else None


_patched = False


def _install_patches() -> None:
    """Bir marta: PTB va boshqa modullarga kichik ulanishlar (boshqa fayllarga tegmasdan)."""
    global _patched
    if _patched:
        return
    _patched = True

    from telegram.ext import ExtBot
    import sticker_store

    # 1) Hamma chiqish xabarlaridagi oddiy emoji -> premium
    if PREMIUM_EMOJI_GLOBAL:
        orig_send = ExtBot.send_message
        orig_edit = ExtBot.edit_message_text

        async def send_message(self, *args, **kwargs):
            new = _premium_kwargs(kwargs)
            if new is not None:
                try:
                    return await orig_send(self, *args, **new)
                except BadRequest as e:
                    logger.warning("premium send_message xato (%s) — oddiy yuborilyapti", e)
            return await orig_send(self, *args, **kwargs)

        async def edit_message_text(self, *args, **kwargs):
            new = _premium_kwargs(kwargs)
            if new is not None:
                try:
                    return await orig_edit(self, *args, **new)
                except BadRequest as e:
                    if "not modified" in str(e).lower():
                        raise
                    logger.warning("premium edit xato (%s) — oddiy yuborilyapti", e)
            return await orig_edit(self, *args, **kwargs)

        ExtBot.send_message = send_message
        ExtBot.edit_message_text = edit_message_text

        def _wrap_caption(name: str) -> None:
            orig = getattr(ExtBot, name, None)
            if orig is None:
                return

            async def wrapper(self, *args, **kwargs):
                new = _premium_kwargs(kwargs, "caption")
                if new is not None:
                    try:
                        return await orig(self, *args, **new)
                    except BadRequest as e:
                        if "not modified" in str(e).lower():
                            raise
                        logger.warning("premium %s xato (%s) — oddiy yuborilyapti", name, e)
                return await orig(self, *args, **kwargs)

            setattr(ExtBot, name, wrapper)

        for _n in ("send_photo", "send_voice", "send_animation", "send_video", "send_audio",
                   "send_document", "edit_message_caption"):
            _wrap_caption(_n)

    # 2) Stiker o'rniga premium emoji
    orig_get_random = sticker_store.get_random

    def get_random(category, *a, **k):
        if (
            category in _MOOD_POOLS
            and _emoji_map()
            and random.random() < PREMIUM_INSTEAD_OF_STICKER
            and any(_norm(e) in _emoji_cache for e in _MOOD_POOLS[category])
        ):
            return _STICKER_MARK + category
        return orig_get_random(category, *a, **k)

    sticker_store.get_random = get_random

    orig_send_sticker = ExtBot.send_sticker

    async def send_sticker(self, chat_id, sticker, *args, **kwargs):
        if isinstance(sticker, str) and sticker.startswith(_STICKER_MARK):
            category = sticker[len(_STICKER_MARK):]
            extra = {k: kwargs[k] for k in ("message_thread_id", "reply_parameters", "business_connection_id")
                     if kwargs.get(k) is not None}
            rich = _mood_pick(category)
            try:
                return await self.send_message(chat_id=chat_id, text=rich or "👍", parse_mode="HTML", **extra)
            except BadRequest:
                return await self.send_message(chat_id=chat_id, text=strip_premium(rich or "👍"), **extra)
        return await orig_send_sticker(self, chat_id, sticker, *args, **kwargs)

    ExtBot.send_sticker = send_sticker

    # 3) AI javoblarida emojilar ko'proq
    orig_get_ai_reply = ai_core.get_ai_reply

    def get_ai_reply(*args, **kwargs):
        # faqat Telegram bot suhbatlari (userbot, sayt va Mini App'ga tegmaydi)
        if kwargs.get("source") == "telegram":
            extra = kwargs.get("extra_system")
            parts = [extra.strip()] if extra and extra.strip() else []
            uid = args[0] if args else kwargs.get("user_id")
            lh = _lang_hint(uid)
            if lh:
                parts.append(lh)
            if not (extra and "ovozli xabar" in extra):          # ovozli javobda emoji kerak emas
                hint = emoji_style_hint()
                if hint:
                    parts.append(hint)
            if parts:
                kwargs["extra_system"] = "\n\n".join(parts)
        return orig_get_ai_reply(*args, **kwargs)

    ai_core.get_ai_reply = get_ai_reply


# ═══════════════════════════════════════════════════════════════════════════
# ▶️ YOUTUBE: havola tanish, video qidirish, YouTuber (kanal) haqida
#    Manba tartibi: YouTube Data API (YOUTUBE_API_KEY bo'lsa) -> yt-dlp -> oEmbed (faqat havola)
# ═══════════════════════════════════════════════════════════════════════════

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
_YT_API = "https://www.googleapis.com/youtube/v3"
_YT_TTL = 900
_yt_cache: dict[str, tuple[float, object]] = {}
_yt_summary: dict[str, str] = {}

_YT_VIDEO_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com/(?:watch\?(?:[^\s&]*&)*v=|shorts/|live/|embed/|v/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})",
    re.I,
)
_YT_CHANNEL_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?youtube\.com/"
    r"(?:(@[\w.\-]+)|channel/(UC[\w-]{22})|c/([\w.\-]+)|user/([\w.\-]+))",
    re.I,
)
_YT_WORD_RE = re.compile(r"(youtube|yutub|ютуб|\bytb\b|\byt\b|video|klip|treyler|trailer)", re.I)
_YT_ACTION_RE = re.compile(
    r"(top\b|topib|qidir|izla|tashla|jo'?nat|yubor|kerak|bormi|\bber\b|ko'?rsat|найди|скинь|find|search|send)", re.I
)
_YT_BER_RE = re.compile(r"(ytber|yutuber|youtuber|yutuberni|yutubchi|bloger|blogger|kanali|kanalini)", re.I)


def _fmt_num(n) -> str:
    if n is None:
        return ""
    n = int(n)
    for div, suf in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= div:
            v = n / div
            return (f"{v:.1f}".rstrip("0").rstrip(".")) + suf
    return str(n)


def _fmt_dur(sec) -> str:
    if not sec:
        return ""
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _iso_dur(s: str | None) -> int:
    m = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    h, mi, se = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + se


def _find_yt_links(text: str) -> list[tuple[str, str]]:
    found = []
    for m in _YT_VIDEO_RE.finditer(text):
        found.append(("video", m.group(1)))
    for m in _YT_CHANNEL_RE.finditer(text):
        ref = next(g for g in m.groups() if g)
        found.append(("channel", ref))
    return found


def _strip_links(text: str) -> str:
    text = _YT_VIDEO_RE.sub(" ", text)
    text = _YT_CHANNEL_RE.sub(" ", text)
    return re.sub(r"\s+", " ", re.sub(r"https?://\S+", " ", text)).strip()


# ---------- YouTube Data API ----------

def _api(path: str, **params) -> dict:
    r = requests.get(f"{_YT_API}/{path}", params={**params, "key": YOUTUBE_API_KEY}, timeout=15)
    r.raise_for_status()
    return r.json()


def _best_thumb(sn: dict) -> str | None:
    th = sn.get("thumbnails") or {}
    for k in ("maxres", "standard", "high", "medium", "default"):
        if k in th:
            return th[k]["url"]
    return None


def _video_from_api(it: dict) -> dict:
    sn, st, cd = it.get("snippet", {}), it.get("statistics", {}), it.get("contentDetails", {})
    return {
        "id": it["id"], "title": sn.get("title", "?"), "channel": sn.get("channelTitle", ""),
        "channel_id": sn.get("channelId"),
        "views": int(st["viewCount"]) if "viewCount" in st else None,
        "likes": int(st["likeCount"]) if "likeCount" in st else None,
        "duration": _iso_dur(cd.get("duration")), "published": (sn.get("publishedAt") or "")[:10],
        "desc": sn.get("description", ""), "thumb": _best_thumb(sn),
        "live": sn.get("liveBroadcastContent") == "live",
    }


def _api_videos(ids: list[str]) -> list[dict]:
    if not ids:
        return []
    data = _api("videos", part="snippet,statistics,contentDetails", id=",".join(ids))
    by_id = {it["id"]: _video_from_api(it) for it in data.get("items", [])}
    return [by_id[i] for i in ids if i in by_id]


def _api_search_videos(q: str, n: int = 5) -> list[dict]:
    data = _api("search", part="snippet", q=q, type="video", maxResults=n, safeSearch="moderate")
    ids = [it["id"]["videoId"] for it in data.get("items", []) if it.get("id", {}).get("videoId")]
    return _api_videos(ids)


def _channel_from_api(it: dict) -> dict:
    sn, st = it.get("snippet", {}), it.get("statistics", {})
    return {
        "id": it["id"], "title": sn.get("title", "?"), "handle": sn.get("customUrl", ""),
        "subs": None if st.get("hiddenSubscriberCount") else (int(st["subscriberCount"]) if "subscriberCount" in st else None),
        "videos": int(st["videoCount"]) if "videoCount" in st else None,
        "views": int(st["viewCount"]) if "viewCount" in st else None,
        "since": (sn.get("publishedAt") or "")[:4], "desc": sn.get("description", ""),
        "thumb": _best_thumb(sn),
        "uploads": ((it.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads"),
    }


def _api_channel(ref_kind: str, ref: str) -> dict | None:
    kw = {"part": "snippet,statistics,contentDetails"}
    if ref_kind == "id":
        kw["id"] = ref
    elif ref_kind == "handle":
        kw["forHandle"] = ref
    elif ref_kind == "user":
        kw["forUsername"] = ref
    data = _api("channels", **kw)
    items = data.get("items") or []
    return _channel_from_api(items[0]) if items else None


def _api_search_channel(q: str) -> dict | None:
    data = _api("search", part="snippet", q=q, type="channel", maxResults=1)
    items = data.get("items") or []
    if not items:
        return None
    return _api_channel("id", items[0]["id"]["channelId"])


def _api_latest(uploads: str | None, n: int = 3) -> list[dict]:
    if not uploads:
        return []
    data = _api("playlistItems", part="snippet", playlistId=uploads, maxResults=n)
    out = []
    for it in data.get("items", []):
        sn = it.get("snippet", {})
        vid = (sn.get("resourceId") or {}).get("videoId")
        if vid:
            out.append({"id": vid, "title": sn.get("title", "?")})
    return out


# ---------- yt-dlp zaxira ----------

def _ydl(flat: bool):
    import yt_dlp

    return yt_dlp.YoutubeDL({
        "quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
        "socket_timeout": 15, "extract_flat": "in_playlist" if flat else False,
    })


def _video_from_dlp(info: dict) -> dict:
    ud = info.get("upload_date") or ""
    thumbs = info.get("thumbnails") or []
    return {
        "id": info.get("id"), "title": info.get("title") or "?",
        "channel": info.get("channel") or info.get("uploader") or "", "channel_id": info.get("channel_id"),
        "views": info.get("view_count"), "likes": info.get("like_count"),
        "duration": info.get("duration") or 0,
        "published": f"{ud[:4]}-{ud[4:6]}-{ud[6:8]}" if len(ud) == 8 else "",
        "desc": info.get("description") or "",
        "thumb": info.get("thumbnail") or (thumbs[-1]["url"] if thumbs else None),
        "live": bool(info.get("is_live")),
    }


def _dlp_video(vid: str) -> dict:
    with _ydl(False) as y:
        return _video_from_dlp(y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False))


def _dlp_search(q: str, n: int = 5) -> list[dict]:
    with _ydl(True) as y:
        info = y.extract_info(f"ytsearch{n}:{q}", download=False)
    out = []
    for e in (info.get("entries") or []):
        if e and e.get("id"):
            out.append(_video_from_dlp(e))
    return out


# ---------- oEmbed (faqat havola uchun, kalitsiz) ----------

def _oembed_video(vid: str) -> dict:
    r = requests.get(
        "https://www.youtube.com/oembed",
        params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"}, timeout=10,
    )
    r.raise_for_status()
    j = r.json()
    return {
        "id": vid, "title": j.get("title", "?"), "channel": j.get("author_name", ""), "channel_id": None,
        "views": None, "likes": None, "duration": 0, "published": "", "desc": "",
        "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", "live": False,
    }


# ---------- yuqori daraja (kesh + zaxira zanjiri) ----------

def _cached(key: str):
    hit = _yt_cache.get(key)
    return hit[1] if hit and time.time() - hit[0] < _YT_TTL else None


def _store(key: str, val):
    if len(_yt_cache) > 400:
        _yt_cache.clear()
    _yt_cache[key] = (time.time(), val)
    return val


async def yt_video(vid: str) -> dict | None:
    if (c := _cached("v:" + vid)) is not None:
        return c
    chain = []
    if YOUTUBE_API_KEY:
        chain.append(lambda: (_api_videos([vid]) or [None])[0])
    chain += [lambda: _dlp_video(vid), lambda: _oembed_video(vid)]
    for fn in chain:
        try:
            v = await asyncio.wait_for(asyncio.to_thread(fn), timeout=25)
            if v:
                return _store("v:" + vid, v)
        except Exception as e:
            logger.warning("yt_video manba xato: %s", e)
    return None


async def yt_search(q: str, n: int = 5) -> list[dict]:
    key = f"s:{n}:{q.lower()}"
    if (c := _cached(key)) is not None:
        return c
    chain = []
    if YOUTUBE_API_KEY:
        chain.append(lambda: _api_search_videos(q, n))
    chain.append(lambda: _dlp_search(q, n))
    for fn in chain:
        try:
            res = await asyncio.wait_for(asyncio.to_thread(fn), timeout=30)
            if res:
                return _store(key, res)
        except Exception as e:
            logger.warning("yt_search manba xato: %s", e)
    return []


async def yt_channel(kind: str, ref: str) -> dict | None:
    """kind: id | handle | user | name"""
    key = f"c:{kind}:{ref.lower()}"
    if (c := _cached(key)) is not None:
        return c
    ch = None
    if YOUTUBE_API_KEY:
        try:
            if kind == "name":
                ch = await asyncio.to_thread(_api_search_channel, ref)
            else:
                ch = await asyncio.to_thread(_api_channel, kind, ref)
            if ch:
                ch["latest"] = await asyncio.to_thread(_api_latest, ch.get("uploads"))
        except Exception as e:
            logger.warning("yt_channel API xato: %s", e)
    if not ch:
        # kalitsiz zaxira: qidiruvdagi eng mos videoning kanali (sodda karta)
        q = ref.lstrip("@")
        vids = await yt_search(q, 5)
        if vids:
            low = q.lower()
            pick = next((v for v in vids if low in (v["channel"] or "").lower()), vids[0])
            mine = [v for v in vids if v["channel_id"] and v["channel_id"] == pick["channel_id"]] or [pick]
            ch = {
                "id": pick.get("channel_id"), "title": pick["channel"] or q, "handle": "", "subs": None,
                "videos": None, "views": None, "since": "", "desc": "", "thumb": pick.get("thumb"),
                "latest": [{"id": v["id"], "title": v["title"]} for v in mine[:3]], "minimal": True,
            }
    return _store(key, ch) if ch else None


async def _uz_summary(key: str, text: str, what: str) -> str:
    if key in _yt_summary:
        return _yt_summary[key]
    text = (text or "").strip()
    if len(text) < 40:
        return ""
    out = await asyncio.to_thread(
        _ai_oneshot,
        f"Sen yordamchisan. Berilgan {what} tavsifidan o'zbek tilida (lotin) 1-2 gapda nima haqida ekanini yoz. "
        "Havolalar, reklama, ijtimoiy tarmoq manzillari va hashtaglarni tashlab ket. Faqat xulosani yoz.",
        text[:900], "",
    )
    _yt_summary[key] = out[:300]
    return _yt_summary[key]


# ---------- kartalar ----------

def _video_caption(v: dict, summary: str) -> str:
    lines = [f"{pe('▶️')} <b>{_esc(v['title'])}</b>"]
    if v.get("channel"):
        lines.append(f"{pe('📺')} {_esc(v['channel'])}")
    facts = []
    if v.get("live"):
        facts.append("🔴 jonli efir")
    if v.get("views") is not None:
        facts.append(f"{pe('👁')} {_fmt_num(v['views'])}")
    if v.get("likes") is not None:
        facts.append(f"{pe('👍')} {_fmt_num(v['likes'])}")
    if v.get("duration"):
        facts.append(f"{pe('⏱')} {_fmt_dur(v['duration'])}")
    if v.get("published"):
        facts.append(f"{pe('📅')} {v['published']}")
    if facts:
        lines.append(" · ".join(facts))
    head = "\n".join(lines)
    if summary:
        room = 1000 - len(head) - 40
        if room > 60:
            body = _esc(summary)
            if len(body) > room:
                body = body[: room - 1].rsplit(" ", 1)[0] + "…"
            head += f"\n\n<blockquote>{body}</blockquote>"
    return head[:1020]


def _video_kb(v: dict, others: list[dict] | None, premium: bool) -> InlineKeyboardMarkup:
    mk = icon_btn if premium else plain_btn
    rows = [[mk("YouTube'da ochish", "▶️", url=f"https://youtu.be/{v['id']}")]]
    if v.get("channel_id"):
        rows[0].append(mk("Kanal", "📺", callback_data=f"yt:ch:{v['channel_id']}"))
    for o in (others or [])[:4]:
        label = f"{o['title'][:34]} · {_fmt_dur(o.get('duration'))}" if o.get("duration") else o["title"][:44]
        rows.append([InlineKeyboardButton(label, callback_data=f"yt:v:{o['id']}")])
    return InlineKeyboardMarkup(rows)


async def _send_card(message, img: str | None, caption: str, kb_factory) -> None:
    attempts = (True, False) if _emoji_map() else (False,)
    last: Exception | None = None
    for premium in attempts:
        cap = caption if premium else strip_premium(caption)
        kb = kb_factory(premium)
        try:
            if img:
                try:
                    await message.reply_photo(photo=img, caption=cap, parse_mode="HTML", reply_markup=kb)
                    return
                except BadRequest as e:
                    if "emoji" in str(e).lower() or "entit" in str(e).lower() or "parse" in str(e).lower():
                        raise
                    logger.warning("rasm yuborilmadi (%s) — matn bilan", e)
            await message.reply_text(cap, parse_mode="HTML", reply_markup=kb)
            return
        except BadRequest as e:
            last = e
    if last:
        raise last


async def send_video_card(message, v: dict, others: list[dict] | None = None) -> None:
    summary = await _uz_summary("v:" + str(v["id"]), v.get("desc", ""), "video")
    await _send_card(message, v.get("thumb"), _video_caption(v, summary), lambda p: _video_kb(v, others, p))


def _channel_caption(c: dict, summary: str) -> str:
    lines = [f"{pe('📺')} <b>{_esc(c['title'])}</b>"]
    if c.get("handle"):
        lines.append(_esc(c["handle"]))
    facts = []
    if c.get("subs") is not None:
        facts.append(f"{pe('👥')} {_fmt_num(c['subs'])} obunachi")
    if c.get("videos") is not None:
        facts.append(f"{pe('🎞')} {_fmt_num(c['videos'])} video")
    if c.get("views") is not None:
        facts.append(f"{pe('👁')} {_fmt_num(c['views'])}")
    if c.get("since"):
        facts.append(f"{pe('📅')} {c['since']}-yildan")
    if facts:
        lines.append(" · ".join(facts))
    head = "\n".join(lines)
    if summary:
        room = 1000 - len(head) - 40
        if room > 60:
            body = _esc(summary)
            if len(body) > room:
                body = body[: room - 1].rsplit(" ", 1)[0] + "…"
            head += f"\n\n<blockquote>{body}</blockquote>"
    if c.get("minimal"):
        head += "\n\n<i>To'liq ma'lumot uchun admin YOUTUBE_API_KEY qo'shishi kerak.</i>"
    return head[:1020]


def _channel_kb(c: dict, premium: bool) -> InlineKeyboardMarkup:
    mk = icon_btn if premium else plain_btn
    url = f"https://www.youtube.com/channel/{c['id']}" if c.get("id") else None
    rows = [[mk("Kanalni ochish", "📺", url=url)]] if url else []
    for v in (c.get("latest") or [])[:3]:
        rows.append([InlineKeyboardButton(v["title"][:44], callback_data=f"yt:v:{v['id']}")])
    return InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([[]])


async def send_channel_card(message, c: dict) -> None:
    summary = await _uz_summary("c:" + str(c.get("id")), c.get("desc", ""), "kanal")
    await _send_card(message, c.get("thumb"), _channel_caption(c, summary), lambda p: _channel_kb(c, p))


# ---------- ishlovchilar ----------

_YT_FAIL = (
    "YouTube hozir javob bermayapti. Birozdan keyin ur.\n"
    "(Admin uchun: Render'ga YOUTUBE_API_KEY qo'shilsa, bu ancha barqaror ishlaydi.)"
)


async def _yt_video_or_search(message, query_or_id: str, is_id: bool) -> None:
    if is_id:
        v = await yt_video(query_or_id)
        if not v:
            await message.reply_text(_YT_FAIL)
            return
        await send_video_card(message, v)
        return
    res = await yt_search(query_or_id, 5)
    if not res:
        await message.reply_text("bunaqa video topilmadi, boshqacha yozib ko'r.")
        return
    first = res[0]
    if not first.get("desc") and first.get("id"):               # yengil (flat) natija — to'liqini olishga urinamiz
        full = await yt_video(first["id"])
        if full:
            first = full
    await send_video_card(message, first, res[1:5])


async def _yt_channel_ref(message, kind: str, ref: str) -> None:
    c = await yt_channel(kind, ref)
    if not c:
        await message.reply_text("kanal topilmadi. nomini aniqroq yoz yoki havolasini yubor.")
        return
    await send_channel_card(message, c)


def _channel_ref_from_link(ref: str) -> tuple[str, str]:
    if ref.startswith("@"):
        return "handle", ref
    if ref.startswith("UC") and len(ref) == 24:
        return "id", ref
    return "name", ref                                         # /c/nom va /user/nom — nom bo'yicha qidiramiz


async def cmd_yt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or admin_store.is_blocked(chat.id):
        return
    arg = " ".join(context.args).strip() if context.args else ""
    if not arg and msg.reply_to_message:
        arg = (msg.reply_to_message.text or "").strip()
    if not arg:
        await msg.reply_text("Nom yoki havola yoz:\n/yt MrBeast eng zo'r videolari\n/yt https://youtu.be/...\n\nYouTuber haqida: /ytber nomi")
        return
    await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
    try:
        links = _find_yt_links(arg)
        if links:
            kind, ref = links[0]
            if kind == "video":
                await _yt_video_or_search(msg, ref, True)
            else:
                await _yt_channel_ref(msg, *_channel_ref_from_link(ref))
        else:
            await _yt_video_or_search(msg, arg[:100], False)
    except Exception:
        logger.exception("/yt xato")
        await msg.reply_text(_YT_FAIL)


async def cmd_ytber(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or admin_store.is_blocked(chat.id):
        return
    arg = " ".join(context.args).strip() if context.args else ""
    if not arg:
        await msg.reply_text("YouTuber nomini yoz:\n/ytber MrBeast\n/ytber @handle")
        return
    await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
    try:
        links = _find_yt_links(arg)
        if links and links[0][0] == "channel":
            await _yt_channel_ref(msg, *_channel_ref_from_link(links[0][1]))
        elif arg.startswith("@") and " " not in arg:
            await _yt_channel_ref(msg, "handle", arg)
        else:
            await _yt_channel_ref(msg, "name", arg[:80])
    except Exception:
        logger.exception("/ytber xato")
        await msg.reply_text(_YT_FAIL)


async def yt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    chat = query.message.chat if query.message else None
    if not chat or admin_store.is_blocked(chat.id):
        await query.answer()
        return
    await query.answer()
    await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
    try:
        if data.startswith("yt:v:"):
            await _yt_video_or_search(query.message, data[5:], True)
        elif data.startswith("yt:ch:"):
            await _yt_channel_ref(query.message, "id", data[6:])
    except Exception:
        logger.exception("yt callback xato")
        await query.message.reply_text(_YT_FAIL)


def _addressed_text(update: Update, bot_username: str | None) -> bool:
    chat, msg = update.effective_chat, update.effective_message
    if not chat or not msg:
        return False
    if chat.type == "private":
        return True
    r = msg.reply_to_message
    if r and r.from_user and r.from_user.is_bot and bot_username and r.from_user.username == bot_username:
        return True
    low = (msg.text or "").lower()
    return "misumi" in low or bool(bot_username and f"@{bot_username.lower()}" in low)


_INTENT_SYSTEM = (
    "Foydalanuvchi xabaridan YouTube qidiruv niyatini aniqla. Faqat JSON qaytar, boshqa hech narsa yozma: "
    '{"intent":"video|channel|none","query":"..."}. '
    "video — biror video, klip, qo'shiq, treyler yoki darslik topib berish so'ralsa. "
    "channel — YouTuber yoki kanalni topish yoki tanishtirish so'ralsa. "
    "Boshqa hamma holatda none. query — YouTube'da qidiriladigan eng yaxshi qisqa so'rov (xabar tilida, "
    "'top', 'ber', 'video' kabi ortiqcha so'zlarsiz, lekin kerak bo'lsa 'video' yoki 'trailer' qolsin)."
)


def _classify_sync(text: str) -> tuple[str, str]:
    import json

    raw = _ai_oneshot(_INTENT_SYSTEM, text[:400], "")
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return "none", ""
    try:
        d = json.loads(m.group(0))
    except Exception:
        return "none", ""
    intent = d.get("intent") if d.get("intent") in ("video", "channel") else "none"
    return intent, str(d.get("query") or "").strip()[:100]


async def handle_text_youtube(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """group=-1: YouTube havolasi yoki 'video top' kabi so'rov bo'lsa o'zi hal qiladi, aks holda
    oddiy suhbatga yo'l beradi (ApplicationHandlerStop faqat hal qilganda)."""
    msg, user, chat = update.effective_message, update.effective_user, update.effective_chat
    text = (msg.text or "").strip() if msg else ""
    if not text or not user or not chat:
        return
    links = _find_yt_links(text)
    intent_hit = bool((_YT_WORD_RE.search(text) and _YT_ACTION_RE.search(text)) or _YT_BER_RE.search(text))
    if not links and not intent_hit:
        return
    if not _addressed_text(update, context.bot.username):
        return
    if admin_store.is_blocked(chat.id) or (admin_store.is_maintenance() and not _is_admin(user.id)):
        return

    if links:
        if not ai_core.check_rate_limit(chat.id, user.id):
            return
        await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
        kind, ref = links[0]
        try:
            if kind == "video":
                await _yt_video_or_search(msg, ref, True)
            else:
                await _yt_channel_ref(msg, *_channel_ref_from_link(ref))
        except Exception:
            logger.exception("YouTube havola xato")
            await msg.reply_text(_YT_FAIL)
        # havola yonida savol ham bo'lsa (shaxsiy chatda) — AI ham javob beradi
        rest = _strip_links(text)
        if chat.type == "private" and len(rest) >= 12 and kind == "video":
            v = await yt_video(ref)
            if v:
                ctx_line = f"[YouTube video: «{v['title']}» — {v['channel']}. Tavsif: {v['desc'][:400]}] "
                try:
                    reply = await asyncio.to_thread(
                        ai_core.get_ai_reply, str(user.id), ctx_line + rest,
                        name=user.first_name, source="telegram",
                    )
                    if reply and reply != "...":
                        await send_ai_reply(update, reply)
                except Exception:
                    logger.exception("havola bo'yicha AI javobi xato")
        raise ApplicationHandlerStop

    # So'rov turi aniq emas — AI dan so'raymiz (rate limit sarflanmaydi; none bo'lsa suhbatga o'tadi)
    intent, q = await asyncio.to_thread(_classify_sync, text)
    if intent == "none" or not q:
        return
    if not ai_core.check_rate_limit(chat.id, user.id):
        raise ApplicationHandlerStop
    await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
    try:
        if intent == "channel":
            await _yt_channel_ref(msg, "handle" if q.startswith("@") and " " not in q else "name", q)
        else:
            await _yt_video_or_search(msg, q, False)
    except Exception:
        logger.exception("YouTube qidiruv xato")
        await msg.reply_text(_YT_FAIL)
    raise ApplicationHandlerStop


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
    app.add_handler(CommandHandler("addpack", cmd_addpack), group=0)
    _install_patches()
    app.add_handler(CallbackQueryHandler(start_callback, pattern=r"^st:"), group=0)
    app.add_handler(CallbackQueryHandler(account_callback, pattern=r"^ac:"), group=0)
    # eski oddiy "Hisob" menyusi o'rniga yangi Hisob bo'limi (orqaga tugmalari ham shunga qaytadi)
    app.add_handler(CallbackQueryHandler(account_callback, pattern=r"^ub:account$"), group=-1)
    app.add_handler(CallbackQueryHandler(imagine_callback, pattern=r"^img:"), group=0)
    app.add_handler(CallbackQueryHandler(tts_callback, pattern=r"^tts:"), group=0)
    app.add_handler(CommandHandler("anime", cmd_anime), group=0)
    app.add_handler(CommandHandler(["yt", "youtube"], cmd_yt), group=0)
    app.add_handler(CommandHandler(["ytber", "kanal"], cmd_ytber), group=0)
    app.add_handler(CallbackQueryHandler(yt_callback, pattern=r"^yt:"), group=0)
    # group=-1: YouTube havola/so'rovlarini oddiy suhbatdan OLDIN ushlaydi (hal qilmasa — o'tkazib yuboradi)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_youtube), group=-1)
    app.add_handler(CallbackQueryHandler(anime_callback, pattern=r"^an:"), group=0)
    app.add_handler(CallbackQueryHandler(photo_callback, pattern=r"^ph:"), group=0)
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice), group=0)
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo), group=0)
    import stickermaker
    stickermaker.register(app)
