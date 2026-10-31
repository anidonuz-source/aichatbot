"""
Misumi AI — downloader.py

⬇️ Video / MP3 yuklab berish (TikTok, Instagram, Facebook, X/Twitter, Pinterest, Vimeo, YouTube).
   /yukla <havola>   — 🎬 Video / 🎵 MP3 tanlash tugmalari chiqadi
   /mp3 <havola>     — to'g'ridan-to'g'ri MP3
   Shaxsiy chatda faqat havolaning o'zini tashlasangiz ham tugmalar chiqadi
   (YouTube havolasi extras dagi kartada chiqadi — u yerda ham yuklash tugmalari bor).

Sozlamalar (env, hammasi ixtiyoriy):
   DL_DAILY_LIMIT     — bir foydalanuvchiga kuniga nechta yuklash (default 20, 0 = cheksiz)
   DL_MAX_MINUTES     — eng uzun video davomiyligi, daqiqada (default 20)
   DL_COOKIES_FILE    — cookies.txt yo'li (Instagram/YouTube serverni bloklasa kerak bo'ladi)

Telegram bot fayl chegarasi — 50 MB, shuning uchun undan kattasi yuborilmaydi.
"""
import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters,
)

import admin_store

logger = logging.getLogger("misumi-downloader")

MAX_BYTES = 49 * 1024 * 1024
MAX_DURATION = int(os.environ.get("DL_MAX_MINUTES", "20") or 20) * 60
DL_DAILY_LIMIT = int(os.environ.get("DL_DAILY_LIMIT", "20") or 0)
DL_COOKIES_FILE = os.environ.get("DL_COOKIES_FILE", "").strip()
COOLDOWN = 8
HAS_FFMPEG = bool(shutil.which("ffmpeg"))

_sem = asyncio.Semaphore(2)               # server bo'sh bo'lib qolmasligi uchun bir vaqtda 2 ta
_links: dict[str, str] = {}               # token -> havola
_last: dict[int, float] = {}
_count: dict[tuple[int, str], int] = {}

_URL_RE = re.compile(r"https?://[^\s<>]+", re.I)
_YT_SITE = r"(?:[\w-]+\.)?(?:youtube\.com|youtu\.be)"
_OTHER_SITE = (
    r"(?:[\w-]+\.)?(?:tiktok\.com|instagram\.com|facebook\.com|fb\.watch|fb\.com|twitter\.com|x\.com|"
    r"pinterest\.[a-z.]+|pin\.it|vimeo\.com)"
)
_ALLOWED_RE = re.compile(rf"^https?://(?:{_YT_SITE}|{_OTHER_SITE})(?:[/?#:]|$)", re.I)
_AUTO_RE = re.compile(rf"^https?://{_OTHER_SITE}(?:[/?#:]|$)", re.I)   # YouTube bu yerda yo'q (extras ushlaydi)


def _today() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=5)).strftime("%Y-%m-%d")   # Toshkent vaqti


def _allowed_user(uid: int) -> tuple[bool, str]:
    now = time.time()
    wait = COOLDOWN - (now - _last.get(uid, 0))
    if wait > 0:
        return False, f"sekin, yana {int(wait) + 1} soniya kut"
    if DL_DAILY_LIMIT > 0 and _count.get((uid, _today()), 0) >= DL_DAILY_LIMIT:
        return False, f"bugungi limit ({DL_DAILY_LIMIT} ta yuklash) tugadi, ertaga qayt"
    return True, ""


def _mark(uid: int) -> None:
    _last[uid] = time.time()
    k = (uid, _today())
    _count[k] = _count.get(k, 0) + 1
    if len(_count) > 5000:
        today = _today()
        for key in [k2 for k2 in _count if k2[1] != today]:
            _count.pop(key, None)


def _first_url(text: str) -> str | None:
    for u in _URL_RE.findall(text or ""):
        u = u.rstrip(".,)!?»”\"'")
        if _ALLOWED_RE.match(u):
            return u
    return None


def _new_token(url: str) -> str:
    tok = uuid.uuid4().hex[:10]
    _links[tok] = url
    if len(_links) > 500:
        for k in list(_links)[:150]:
            _links.pop(k, None)
    return tok


def _resolve(tok: str) -> str | None:
    if tok.startswith("y") and re.fullmatch(r"y[A-Za-z0-9_-]{11}", tok):   # YouTube kartasidan
        return f"https://www.youtube.com/watch?v={tok[1:]}"
    return _links.get(tok)


def _choice_kb(tok: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎬 Video", callback_data=f"dl:v:{tok}"),
        InlineKeyboardButton("🎵 MP3", callback_data=f"dl:a:{tok}"),
    ]])


# ═══════════════════════════ yuklab olish (yt-dlp) ═══════════════════════════

class DownloadError(Exception):
    pass


def _download_sync(url: str, audio: bool, outdir: str) -> dict:
    import yt_dlp

    skipped: dict[str, str] = {}

    def _filter(info, *, incomplete=False):
        d = info.get("duration")
        if d and d > MAX_DURATION:
            skipped["why"] = "long"
            return "too long"
        return None

    opts: dict = {
        "outtmpl": os.path.join(outdir, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "max_filesize": MAX_BYTES,
        "socket_timeout": 30,
        "retries": 2,
        "match_filter": _filter,
    }
    if DL_COOKIES_FILE and os.path.exists(DL_COOKIES_FILE):
        opts["cookiefile"] = DL_COOKIES_FILE

    if audio:
        if HAS_FFMPEG:
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
        else:
            opts["format"] = "bestaudio[ext=m4a]/bestaudio"
    else:
        # bitta fayl (ffmpeg birlashtirishsiz) — tezroq va 50 MB ichiga sig'adi
        opts["format"] = "best[ext=mp4][height<=720]/best[ext=mp4]/best[height<=720]/best"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        raise DownloadError(str(e)) from e

    if not info:
        raise DownloadError("long" if skipped.get("why") == "long" else "empty")
    if info.get("_type") == "playlist" and info.get("entries"):
        info = next((e for e in info["entries"] if e), info)

    files = [
        os.path.join(outdir, f) for f in os.listdir(outdir)
        if not f.endswith((".part", ".ytdl", ".temp"))
    ]
    if not files:
        raise DownloadError("empty")
    path = max(files, key=os.path.getsize)
    size = os.path.getsize(path)
    if size > MAX_BYTES:
        raise DownloadError("big")
    return {
        "path": path,
        "title": (info.get("title") or "")[:200],
        "uploader": (info.get("uploader") or info.get("channel") or "")[:100],
        "duration": int(info.get("duration") or 0) or None,
        "width": info.get("width"),
        "height": info.get("height"),
    }


def _error_text(err: str) -> str:
    low = (err or "").lower()
    if err == "long" or "too long" in low:
        return f"bu video juda uzun (maks. {MAX_DURATION // 60} daqiqa)."
    if err == "big" or "larger than" in low or "max-filesize" in low:
        return "fayl 50 MB dan katta — Telegram bot buni yubora olmaydi. Qisqaroq video sinab ko'r."
    if any(w in low for w in ("private", "login", "sign in", "cookies", "age", "rate-limit", "rate limit")):
        return "bu videoni yuklab bo'lmadi (yopiq akkaunt yoki sayt serverni to'sib qo'ydi). Boshqa havola sinab ko'r."
    return "yuklab bo'lmadi. Havola to'g'rimi? Keyinroq qayta urinib ko'r."


async def _do_download(message, user, chat, url: str, audio: bool, context) -> None:
    ok, why = _allowed_user(user.id)
    if not ok:
        await message.reply_text(why)
        return
    _mark(user.id)
    status = await message.reply_text("⬇️ yuklab olyapman... (biroz kuting)")
    tmp = tempfile.mkdtemp(prefix="dl_")
    try:
        async with _sem:
            await context.bot.send_chat_action(
                chat_id=chat.id, action=ChatAction.UPLOAD_AUDIO if audio else ChatAction.UPLOAD_VIDEO,
            )
            try:
                res = await asyncio.to_thread(_download_sync, url, audio, tmp)
            except DownloadError as e:
                logger.warning("yuklash xatosi (%s): %s", url, e)
                await status.edit_text(_error_text(str(e)))
                return
            except Exception:
                logger.exception("kutilmagan yuklash xatosi")
                await status.edit_text(_error_text(""))
                return

            await context.bot.send_chat_action(
                chat_id=chat.id, action=ChatAction.UPLOAD_AUDIO if audio else ChatAction.UPLOAD_VIDEO,
            )
            try:
                with open(res["path"], "rb") as fh:
                    if audio:
                        await message.reply_audio(
                            audio=fh, title=res["title"] or None, performer=res["uploader"] or None,
                            duration=res["duration"], filename=os.path.basename(res["path"]),
                            read_timeout=180, write_timeout=180, connect_timeout=30,
                        )
                    else:
                        await message.reply_video(
                            video=fh, caption=(res["title"] or "")[:200] or None,
                            duration=res["duration"], width=res["width"], height=res["height"],
                            supports_streaming=True,
                            read_timeout=180, write_timeout=180, connect_timeout=30,
                        )
            except TelegramError:
                logger.exception("Telegramga yuborib bo'lmadi")
                await status.edit_text("yuklandi-yu, lekin Telegramga yuborib bo'lmadi. Qayta urinib ko'r.")
                return
            try:
                await status.delete()
            except TelegramError:
                pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ═══════════════════════════ handlerlar ═══════════════════════════

def _blocked(update: Update, user) -> bool:
    chat = update.effective_chat
    if admin_store.is_blocked(chat.id):
        return True
    try:
        import extras
        return admin_store.is_maintenance() and not extras._is_admin(user.id)
    except Exception:
        return False


async def cmd_yukla(update: Update, context: ContextTypes.DEFAULT_TYPE, force_audio: bool = False):
    msg, user, chat = update.effective_message, update.effective_user, update.effective_chat
    if not msg or not user or not chat or _blocked(update, user):
        return
    raw = msg.text or ""
    url = _first_url(raw)
    if not url and msg.reply_to_message:
        url = _first_url(msg.reply_to_message.text or msg.reply_to_message.caption or "")
    if not url:
        await msg.reply_text(
            "Havola yubor:\n/yukla https://www.tiktok.com/...\n/mp3 https://youtu.be/...\n\n"
            "Qo'llanadi: TikTok, Instagram, YouTube, Facebook, X (Twitter), Pinterest, Vimeo."
        )
        return
    if force_audio:
        await _do_download(msg, user, chat, url, True, context)
        return
    await msg.reply_text("Nimani yuklay?", reply_markup=_choice_kb(_new_token(url)))


async def cmd_mp3(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_yukla(update, context, force_audio=True)


async def link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shaxsiy chatda faqat havola tashlansa (TikTok/Instagram/...) — yuklash tugmalarini ko'rsatadi.
    Gap ichidagi havolalarga tegmaydi (oddiy suhbat davom etadi). YouTube — extras kartasida."""
    msg, user, chat = update.effective_message, update.effective_user, update.effective_chat
    if not msg or not msg.text or not user or not chat or chat.type != "private":
        return
    text = msg.text.strip()
    m = _URL_RE.search(text)
    if not m:
        return
    url = m.group(0).rstrip(".,)!?»”\"'")
    if not _AUTO_RE.match(url) or len(text) > len(url) + 12:
        return
    if _blocked(update, user):
        return
    await msg.reply_text("Nimani yuklay?", reply_markup=_choice_kb(_new_token(url)))
    raise ApplicationHandlerStop


async def dl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user, chat = query.from_user, update.effective_chat
    parts = (query.data or "").split(":", 2)
    if len(parts) != 3 or parts[1] not in ("v", "a"):
        await query.answer()
        return
    url = _resolve(parts[2])
    if not url:
        await query.answer("havola eskirdi, qaytadan yubor", show_alert=True)
        return
    ok, why = _allowed_user(user.id)
    if not ok:
        await query.answer(why, show_alert=True)
        return
    await query.answer("⬇️ boshladim")
    await _do_download(query.message, user, chat, url, parts[1] == "a", context)


def register(app: Application) -> None:
    app.add_handler(CommandHandler(["yukla", "download", "dl"], cmd_yukla), group=0)
    app.add_handler(CommandHandler(["mp3", "musiqa"], cmd_mp3), group=0)
    # Har bir group da faqat BIRINCHI mos handler ishlaydi. -1 da extras.handle_text_youtube, -2 da
    # stickermaker.nl_sticker hamma matnni ushlaydi — shuning uchun bu handler alohida group=-3 da.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, link_handler), group=-3)
    app.add_handler(CallbackQueryHandler(dl_callback, pattern=r"^dl:"), group=0)
