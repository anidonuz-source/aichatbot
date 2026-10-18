"""
Misumi AI — stickermaker.py

🧩 Stiker yasash:
   /stikeryasa              — rasmga reply qilib yoki rasm + sarlavha bilan: rasm -> 512px WEBP stiker
   /stikeryasa <tavsif>     — matndan AI stiker chizadi (fon avtomatik olib tashlanadi)
   /stikerpaket             — shaxsiy stiker paketingiz havolasi
   Stiker tagidagi tugmalar: ➕ paketga qo'shish, ✂️ fonni olib tashlash / 🖼 fon bilan

Fonni olib tashlash (og'ir kutubxonasiz, Render bepul tarifiga mos):
   1) REMOVEBG_API_KEY bo'lsa — remove.bg (eng sifatli, oyiga 50 ta bepul)
   2) bo'lmasa — Pillow bilan chetdan to'ldirish (bir xil rangli fonda yaxshi ishlaydi)
"""
import asyncio
import io
import logging
import os
import re
import time

import requests
from PIL import Image, ImageChops, ImageDraw, ImageFilter
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputSticker, Update
from telegram.constants import ChatAction, StickerFormat
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters,
)

import admin_store

logger = logging.getLogger("misumi-stickermaker")

REMOVEBG_API_KEY = os.environ.get("REMOVEBG_API_KEY", "").strip()
MAX_PACK = 120                       # Telegram: statik paketda 120 tagacha
DEFAULT_EMOJI = "😀"
MAX_WEBP_BYTES = 500 * 1024          # Telegram chegarasi 512 KB
_cache: dict[str, dict] = {}         # token -> {"uid", "orig", "cut"}
_make_last: dict[int, float] = {}
MAKE_COOLDOWN = 4


# ═══════════════════════════ rasm bilan ishlash ═══════════════════════════

def _load(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")
    # juda katta rasmni oldindan kichraytiramiz (xotira tejash)
    if max(img.size) > 1600:
        img.thumbnail((1600, 1600), Image.LANCZOS)
    return img


def _border_is_uniform(rgb: Image.Image, tol: int = 60) -> bool:
    """Chetdagi piksellar bir-biriga yaqin rangdami (fon bir xil bo'lsa — flood-fill ishlaydi)."""
    w, h = rgb.size
    pts = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1),
           (w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2)]
    cols = [rgb.getpixel(p) for p in pts]
    ref = cols[0]
    return all(max(abs(c[i] - ref[i]) for i in range(3)) <= tol for c in cols)


def remove_bg_local(img: Image.Image, thresh: int = 38) -> Image.Image | None:
    """Chetdan flood-fill bilan fonni shaffof qiladi. Fon murakkab bo'lsa None qaytaradi."""
    rgba = img.convert("RGBA")
    # allaqachon shaffof rasm — tegmaymiz
    if rgba.getchannel("A").getextrema()[0] < 250:
        return rgba
    # tez ishlashi uchun 512 atrofida ishlaymiz
    work = rgba.copy()
    work.thumbnail((512, 512), Image.LANCZOS)
    rgb = work.convert("RGB")
    if not _border_is_uniform(rgb):
        return None
    marked = rgb.copy()
    marker = (255, 0, 255)
    w, h = marked.size
    seeds = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1),
             (w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2)]
    for pt in seeds:
        if marked.getpixel(pt) != marker:
            ImageDraw.floodfill(marked, pt, marker, thresh=thresh)
    diff = ImageChops.difference(marked, rgb).convert("L")
    bg_mask = diff.point(lambda v: 255 if v > 0 else 0)       # fon = 255
    alpha = ImageChops.invert(bg_mask)                        # obyekt = 255
    # obyekt juda kichik / juda katta qolsa — natija ishonchsiz
    hist = alpha.histogram()
    fg_ratio = hist[255] / float(w * h)
    if fg_ratio < 0.03 or fg_ratio > 0.97:
        return None
    # chetlarni yumshatish va ozgina yeb qo'yish (oq "halo" qolmasin)
    alpha = alpha.filter(ImageFilter.MedianFilter(5))
    alpha = alpha.filter(ImageFilter.MinFilter(3))
    alpha = alpha.filter(ImageFilter.GaussianBlur(1.0))
    alpha = alpha.resize(rgba.size, Image.LANCZOS)
    out = rgba.copy()
    out.putalpha(alpha)
    return out


def remove_bg_api(data: bytes) -> Image.Image | None:
    if not REMOVEBG_API_KEY:
        return None
    try:
        r = requests.post(
            "https://api.remove.bg/v1.0/removebg",
            files={"image_file": ("img.png", data)},
            data={"size": "auto"},
            headers={"X-Api-Key": REMOVEBG_API_KEY},
            timeout=60,
        )
        if r.status_code != 200:
            logger.warning("remove.bg %s: %s", r.status_code, r.text[:200])
            return None
        out = Image.open(io.BytesIO(r.content))
        out.load()
        return out.convert("RGBA")
    except Exception:
        logger.exception("remove.bg xato")
        return None


def to_sticker_webp(img: Image.Image) -> bytes:
    """Telegram statik stiker: bir tomoni aynan 512 px, WEBP, <512 KB."""
    img = img.convert("RGBA")
    bbox = img.getchannel("A").getbbox()
    if bbox:
        pad = max(4, int(max(img.size) * 0.02))
        l, t, r, b = bbox
        img = img.crop((max(0, l - pad), max(0, t - pad), min(img.width, r + pad), min(img.height, b + pad)))
    w, h = img.size
    scale = 512 / max(w, h)
    img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    for q in (95, 85, 75, 60, 45):
        buf = io.BytesIO()
        img.save(buf, "WEBP", quality=q, method=6)
        if buf.tell() <= MAX_WEBP_BYTES:
            return buf.getvalue()
    return buf.getvalue()


def _process_sync(data: bytes, cut: bool) -> tuple[bytes, bool]:
    """(webp, fon_olindimi). cut=False bo'lsa faqat o'lchamlash."""
    img = _load(data)
    removed = False
    if cut:
        out = remove_bg_api(data) if REMOVEBG_API_KEY else None
        if out is None:
            out = remove_bg_local(img)
        if out is not None:
            img, removed = out, True
    return to_sticker_webp(img), removed


# ═══════════════════════════ matnlar (uz / ru / en) ═══════════════════════════

_T = {
    "help": (
        "🧩 Stiker yasash:\n• rasmga reply qilib /stikeryasa yoz\n• yoki /stikeryasa kuchukcha, anime uslubida — AI o'zi chizadi",
        "🧩 Создание стикера:\n• ответь на фото командой /stikeryasa\n• или /stikeryasa щенок в стиле аниме — ИИ нарисует сам",
        "🧩 Make a sticker:\n• reply to a photo with /stikeryasa\n• or /stikeryasa a puppy, anime style — AI will draw it",
    ),
    "making": ("🧩 stiker yasayapman...", "🧩 делаю стикер...", "🧩 making your sticker..."),
    "fail": (
        "stiker chiqmadi, keyinroq ur.",
        "не получилось, попробуй позже.",
        "couldn't make it, try again later.",
    ),
    "nobg": (
        "fon murakkab ekan, olib tashlay olmadim (bir xil rangli fonda yaxshi ishlaydi).",
        "фон сложный, убрать не смог (лучше работает на однотонном фоне).",
        "background is too complex to remove (works best on plain backgrounds).",
    ),
    "added": ("✅ paketga qo'shildi!", "✅ добавлено в пак!", "✅ added to your pack!"),
    "created": ("🎉 paketing yaratildi!", "🎉 пак создан!", "🎉 your pack is created!"),
    "open": ("📦 Paketni ochish", "📦 Открыть пак", "📦 Open pack"),
    "add": ("➕ Paketga qo'shish", "➕ В пак", "➕ Add to pack"),
    "cut": ("✂️ Fonsiz", "✂️ Без фона", "✂️ No background"),
    "keep": ("🖼 Fon bilan", "🖼 С фоном", "🖼 With background"),
    "old": (
        "eskirib qoldi, /stikeryasa ni qaytadan ishlat",
        "устарело, запусти /stikeryasa заново",
        "expired, run /stikeryasa again",
    ),
    "notyours": ("bu tugma senga emas", "эта кнопка не для тебя", "this button isn't yours"),
    "full": (
        "paket to'ldi (120 ta). yangisini boshlash uchun eskisini tozala.",
        "пак заполнен (120 шт).",
        "your pack is full (120 stickers).",
    ),
    "nopack": (
        "hali paketing yo'q. /stikeryasa bilan stiker yasab, ➕ tugmasini bos.",
        "пака ещё нет. сделай стикер через /stikeryasa и нажми ➕.",
        "you don't have a pack yet. Make a sticker and tap ➕.",
    ),
    "pack": ("📦 Sening paketing:", "📦 Твой пак:", "📦 Your pack:"),
    "wait": ("sekin, bir oz kut", "помедленнее", "slow down a bit"),
    "startbot": (
        "avval menga shaxsiyda /start yoz, keyin paket yarata olaman.",
        "сначала напиши мне /start в личку.",
        "message me /start in private first.",
    ),
}


def _lang(user_id) -> int:
    try:
        import extras
        return {"uz": 0, "ru": 1, "en": 2}.get(extras._get_lang(user_id), 0)
    except Exception:
        return 0


def t(user_id, key: str) -> str:
    return _T[key][_lang(user_id)]


# ═══════════════════════════ paket (Telegram sticker set) ═══════════════════════════

def _pack_name(user_id: int, bot_username: str) -> str:
    return f"m{user_id}_by_{bot_username.lower()}"


async def _add_to_pack(bot, user, webp: bytes) -> tuple[str, bool]:
    """(paket nomi, yangi_yaratildimi). Paket yo'q bo'lsa yaratadi, bor bo'lsa qo'shadi."""
    name = _pack_name(user.id, bot.username)
    sticker = InputSticker(sticker=webp, emoji_list=[DEFAULT_EMOJI], format=StickerFormat.STATIC)
    try:
        pack = await bot.get_sticker_set(name)
    except TelegramError:
        pack = None
    if pack is None:
        title = f"{(user.first_name or 'User')[:30]} • Misumi"
        await bot.create_new_sticker_set(user_id=user.id, name=name, title=title, stickers=[sticker])
        return name, True
    if len(pack.stickers) >= MAX_PACK:
        raise OverflowError("pack full")
    await bot.add_sticker_to_set(user_id=user.id, name=name, sticker=sticker)
    return name, False


def _kb(token: str, uid, cut: bool) -> InlineKeyboardMarkup:
    toggle = InlineKeyboardButton(t(uid, "keep") if cut else t(uid, "cut"), callback_data=f"sk:bg:{token}")
    add = InlineKeyboardButton(t(uid, "add"), callback_data=f"sk:add:{token}")
    return InlineKeyboardMarkup([[add], [toggle]])


# ═══════════════════════════ asosiy oqim ═══════════════════════════

def _new_token(uid: int) -> str:
    tok = f"{uid}{int(time.time())}"[-12:]
    if len(_cache) > 300:
        for k in list(_cache)[:100]:
            _cache.pop(k, None)
    return tok


async def _send_sticker(message, user, orig: bytes, cut: bool, token: str | None = None) -> None:
    uid = user.id
    try:
        webp, removed = await asyncio.to_thread(_process_sync, orig, cut)
    except Exception:
        logger.exception("stiker tayyorlashda xato")
        await message.reply_text(t(uid, "fail"))
        return
    token = token or _new_token(uid)
    _cache[token] = {"uid": uid, "orig": orig, "cut": removed}
    await message.reply_sticker(sticker=webp, reply_markup=_kb(token, uid, removed))
    if cut and not removed:
        await message.reply_text(t(uid, "nobg"))


async def _download_photo(message, bot) -> bytes | None:
    """Xabarning o'zidagi yoki reply qilingan rasm / rasm-fayl / stikerni yuklaydi."""
    for m in (message, message.reply_to_message):
        if not m:
            continue
        tg = None
        if m.photo:
            tg = m.photo[-1]
        elif m.document and (m.document.mime_type or "").startswith("image/"):
            tg = m.document
        elif m.sticker and not m.sticker.is_animated and not m.sticker.is_video:
            tg = m.sticker
        if tg:
            f = await bot.get_file(tg.file_id)
            return bytes(await f.download_as_bytearray())
    return None


def _rate_ok(uid: int) -> bool:
    now = time.time()
    if now - _make_last.get(uid, 0) < MAKE_COOLDOWN:
        return False
    _make_last[uid] = now
    return True


async def _make_from_text(message, user, chat, prompt: str, context) -> None:
    import extras
    ok, why = extras._imagine_allowed(user.id)
    if not ok:
        await message.reply_text(why)
        return
    extras._imagine_mark(user.id)
    await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.UPLOAD_PHOTO)
    status = await message.reply_text(t(user.id, "making") + " (30-60s)")
    full = (
        f"{prompt}. Cute die-cut sticker illustration, bold clean outline, vibrant colors, "
        "single subject centered, on a flat solid pure white background, no shadow, no text, no border."
    )
    try:
        data, _provider = await extras._generate(full)
    except Exception:
        logger.exception("AI stiker chizishda xato")
        await status.edit_text(t(user.id, "fail"))
        return
    try:
        await _send_sticker(message, user, data, True)
    finally:
        try:
            await status.delete()
        except TelegramError:
            pass


async def cmd_stikeryasa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user, chat = update.effective_message, update.effective_user, update.effective_chat
    if not msg or not user or not chat or admin_store.is_blocked(chat.id):
        return
    if not _rate_ok(user.id):
        await msg.reply_text(t(user.id, "wait"))
        return
    # sarlavhali rasmda args bo'lmaydi — matnni sarlavhadan olamiz
    raw = msg.text or msg.caption or ""
    prompt = re.sub(r"^/\w+(@\w+)?\s*", "", raw).strip()
    orig = None
    try:
        orig = await _download_photo(msg, context.bot)
    except Exception:
        logger.exception("rasm yuklab bo'lmadi")
    if orig:
        await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.UPLOAD_PHOTO)
        await _send_sticker(msg, user, orig, True)
        return
    if prompt:
        await _make_from_text(msg, user, chat, prompt[:300], context)
        return
    await msg.reply_text(t(user.id, "help"))


async def sk_photo_button(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str) -> None:
    """Rasm menyusidagi '🧩 Stiker' tugmasi (extras.photo_callback shu yerga yo'naltiradi)."""
    query = update.callback_query
    user = query.from_user
    if not _rate_ok(user.id):
        await query.answer(t(user.id, "wait"), show_alert=True)
        return
    await query.answer(t(user.id, "making"))
    f = await context.bot.get_file(file_id)
    orig = bytes(await f.download_as_bytearray())
    await _send_sticker(query.message, user, orig, True)


async def sk_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        await query.answer()
        return
    action, token = parts[1], parts[2]
    if action == "photo":
        import extras
        fid = extras._photo_cache.get(token)
        if not fid:
            await query.answer(t(user.id, "old"), show_alert=True)
            return
        await sk_photo_button(update, context, fid)
        return
    item = _cache.get(token)
    if not item:
        await query.answer(t(user.id, "old"), show_alert=True)
        return
    if item["uid"] != user.id:
        await query.answer(t(user.id, "notyours"), show_alert=True)
        return

    if action == "bg":
        await query.answer(t(user.id, "making"))
        want_cut = not item["cut"]
        try:
            await query.message.delete()
        except TelegramError:
            pass
        await _send_sticker(query.message, user, item["orig"], want_cut, token)
        return

    if action == "add":
        try:
            webp, _ = await asyncio.to_thread(_process_sync, item["orig"], item["cut"])
            name, created = await _add_to_pack(context.bot, user, webp)
        except OverflowError:
            await query.answer(t(user.id, "full"), show_alert=True)
            return
        except BadRequest as e:
            logger.warning("paketga qo'shishda xato: %s", e)
            await query.answer(t(user.id, "startbot"), show_alert=True)
            return
        except Exception:
            logger.exception("paketga qo'shishda xato")
            await query.answer(t(user.id, "fail"), show_alert=True)
            return
        await query.answer(t(user.id, "created") if created else t(user.id, "added"))
        link = f"https://t.me/addstickers/{name}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(t(user.id, "open"), url=link)]])
        try:
            await query.edit_message_reply_markup(reply_markup=kb)
        except TelegramError:
            await query.message.reply_text(link)


async def cmd_stikerpaket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user = update.effective_message, update.effective_user
    if not msg or not user:
        return
    name = _pack_name(user.id, context.bot.username)
    try:
        await context.bot.get_sticker_set(name)
    except TelegramError:
        await msg.reply_text(t(user.id, "nopack"))
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(t(user.id, "open"), url=f"https://t.me/addstickers/{name}")]])
    await msg.reply_text(t(user.id, "pack"), reply_markup=kb)


async def caption_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Rasm sarlavhasidagi /stikeryasa — oddiy rasm tahlili handleriga o'tib ketmasin."""
    await cmd_stikeryasa(update, context)
    raise ApplicationHandlerStop


def register(app: Application) -> None:
    app.add_handler(CommandHandler(["stikeryasa", "stikerqil", "makesticker"], cmd_stikeryasa), group=0)
    app.add_handler(CommandHandler(["stikerpaket", "mypack"], cmd_stikerpaket), group=0)
    # rasm + sarlavha "/stikeryasa" (CommandHandler sarlavhani ko'rmaydi)
    app.add_handler(
        MessageHandler(
            (filters.PHOTO | filters.Document.IMAGE) & filters.CaptionRegex(r"^/(stikeryasa|stikerqil|makesticker)"),
            caption_cmd,
        ),
        group=-1,
    )
    app.add_handler(CallbackQueryHandler(sk_callback, pattern=r"^sk:"), group=0)
