"""
Misumi AI — study.py

Universal study/homework helper — any subject, any grade level.

  /dars <savol>          — matnli savol yoki masala: to'liq, qadam-baqadam yechim
  /dars (rasmga reply)   — rasmdagi masala/savolni o'qib yechadi
  Rasm yuborib chiqqan menyudan "🧮 Masalani yech" tugmasi — xuddi shunday,
  qo'shimcha komandasiz.

Matematika (algebra, geometriya, trigonometriya, hosila/integral), fizika,
kimyo, biologiya, tarix, adabiyot, grammatika, tilni o'rganish, insho — fan
farqi yo'q, hammasida bir xil sifatda ishlaydi. Javob hech qachon
qisqartirib kesilmaydi: uzun chiqsa, bir necha xabarga bo'lib yuboriladi.

Ulanishi (bot.py ga bitta qo'shimcha bilan):
    import study
    ...
    study.register(app)

Boshqa fayllarga tegilmaydi — rasm menyusiga tugma qo'shish
`extras._photo_menu_kb` ni extras.py o'z ichida qilgan kichik "patch"lar
uslubida, shu faylning ichida amalga oshiriladi.
"""
import asyncio
import logging

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import admin_store
import ai_core
import extras

logger = logging.getLogger(__name__)

_patched = False


# ═══════════════════════════════════════════════════════════════════════════
# 🧠 Universal "domla" ko'rsatmasi — har qanday fandan savolni yechadi
# ═══════════════════════════════════════════════════════════════════════════

STUDY_SYSTEM = """[REJIM: DARS YORDAMCHISI] Bu ko'rsatma yuqoridagi barcha uslub
(ko'cha gapi, kesatish, qisqalik, STICKER/REACT va XOTIRA teglari) ko'rsatmalaridan
USTUN. Hozir sen hartomonlama bilimli, sabrli domlasan — kesatgich emas, chinakam
ustoz.

Vazifa: foydalanuvchi yuborgan savol, masala yoki topshiriqni YECH yoki
TUSHUNTIR. Fan farqi yo'q — matematika (algebra, geometriya, trigonometriya,
hosila, integral, ehtimollar), fizika, kimyo (reaksiya tenglamalari,
stekiometriya), biologiya, tarix, geografiya, adabiyot, ona tili/grammatika,
chet tili, informatika, insho/referat — barchasida bir xil yuqori sifatda
ishla. Sen oddiy chatbot emassan, istalgan fandan alo darajada javob
beradigan universal domlasan.

QATTIQ QOIDALAR:
1. Fan va mavzuni avval bitta qisqa qatorda ayt (masalan: "📐 Fan: Geometriya
   — uchburchak yuzi").
2. Yechimni TO'LIQ, qadam-baqadam ko'rsat: formulani yoz, sonlarni
   formulaga qo'yib hisobla, har bir qadamni ko'rsat. Qadam tashlab ketma.
3. Oxirida alohida qatorda, aniq ko'rinadigan qilib: "✅ Javob: ..." —
   yakuniy javob.
4. Matematik ifodalarni oddiy matnda aniq yoz (masalan: x^2 + 5x - 6 = 0,
   yoki integral(x^2 dx)) — chalkash maxsus belgi ishlatma.
5. Savol yoki rasm noaniq bo'lsa — nimani qanday tushunganingni bir og'iz
   ayt, shunga asoslanib eng mantiqiy yechimni ber, shubha bo'lsa aytib o't.
6. Javobni hech qachon qisqartirib tashlama yoki "..." bilan kesma — uzun
   bo'lsa ham oxirigacha to'liq yoz.
7. Savol qaysi tilda yozilgan bo'lsa (o'zbek, rus, ingliz), javobni o'sha
   tilda ber; aks holda o'zbekcha javob ber.
8. Emoji juda kam, faqat fan belgilash uchun (📐 🧪 🧬 📖 🖋 ⚗️ 🌍 va sh.k.) —
   matn ichida ortiqcha emoji yo'q.
9. STICKER, REACT va ⟦MEMORY:...⟧ teglarini ishlatma.
10. Oxirida bitta qisqa, samimiy gap bilan qo'shimcha yordam taklif qil
    (masalan: "yana savol bo'lsa, shuni ham yoz."). Ko'cha uslubida emas,
    oddiy ustozcha ohangda.
"""

STUDY_SYSTEM_PHOTO = STUDY_SYSTEM + """

Bu safar savol RASM ko'rinishida keldi. Avval rasmda nima yozilgan yoki
chizilganini bir qatorda ayt (masalan: "📷 Rasmda: ... tenglama/masala
ko'ryapman"), keyin yuqoridagi qoidalar bo'yicha to'liq yech. Qo'lyozma xira
yoki chala bo'lsa, eng mantiqiy o'qilishni tanlab yech va buni aytib o't.
"""

USAGE = (
    "📚 <b>Dars yordamchisi</b>\n\n"
    "Istalgan fandan savol yoki masalani yuboring — to'liq, qadam-baqadam "
    "yechib beraman. Matematika, fizika, kimyo, biologiya, tarix, adabiyot, "
    "grammatika — fan farqi yo'q.\n\n"
    "• <code>/dars 2x + 5 = 17 tenglamani yech</code>\n"
    "• Masala yozilgan rasmga <b>reply</b> qilib <code>/dars</code> deb yozing\n"
    "• Yoki rasmni shunchaki yuboring — chiqqan tugmalardan "
    "<b>🧮 Masalani yech</b> ni bosing\n\n"
    "📄 PDF, .txt yoki rasm-fayl yuboring — mazmunini qisqacha aytib beraman "
    "yoki tagiga savolingizni yozsangiz, shunga javob beraman."
)


# ═══════════════════════════════════════════════════════════════════════════
# 📄 Hujjat/PDF o'qish — fayl yoki rasm-fayl yuborilsa, mazmunini ochib beradi
# ═══════════════════════════════════════════════════════════════════════════

DOC_SYSTEM = """[REJIM: HUJJAT TAHLILI] Bu ko'rsatma yuqoridagi barcha uslub
(ko'cha gapi, kesatish, qisqalik, STICKER/REACT va XOTIRA teglari) ko'rsatmalaridan
USTUN. Foydalanuvchi senga fayl (hujjat) yubordi — sen uni to'liq o'qib chiqqan
professional yordamchisan.

Vazifa:
- Foydalanuvchi savol/topshiriq yozmagan bo'lsa — hujjatning qisqacha
  mazmunini ber: nima haqida, asosiy 3-5 fikr, xulosa.
- Savol yoki topshiriq yozgan bo'lsa — aynan o'shaga, hujjat matniga
  asoslanib to'liq javob ber. Hujjatda yo'q narsani o'ylab topma — agar
  javob hujjatda topilmasa, shuni ochiq ayt.
- Kerak bo'lsa hujjatdan qisqa parcha keltirishing mumkin, lekin asosan o'z
  so'zlaring bilan tushuntir.
- Javobni hech qachon qisqartirib tashlama — uzun bo'lsa ham to'liq yoz.
- Hujjat qaysi tilda bo'lsa, foydalanuvchi boshqacha so'ramaguncha o'sha
  tilda (yoki foydalanuvchi yozgan tilda) javob ber.
- STICKER, REACT va ⟦MEMORY:...⟧ teglarini ishlatma. Oddiy, samimiy va
  aniq ohangda yoz — ko'cha uslubisiz.
"""

_DOC_MAX_BYTES = 20 * 1024 * 1024  # Telegram bot API faylni shuncha yuklaydi
_DOC_TEXT_LIMIT = 60_000            # .txt faylni shuncha belgigacha o'qiymiz

_SUPPORTED_DOC_MIMES = {"application/pdf", "text/plain"}


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not msg.document or not user or not chat:
        return
    if not extras._should_answer(update, context.bot.username):
        return
    if not extras._gate(update):
        return

    doc = msg.document
    mime = (doc.mime_type or "").lower()
    caption = (msg.caption or "").strip()
    is_image = mime.startswith("image/")

    if not (mime in _SUPPORTED_DOC_MIMES or is_image):
        if chat.type == "private":
            await msg.reply_text(
                "hozircha faqat PDF, oddiy matn (.txt) va rasm-fayllarni o'qiy olaman."
            )
        return

    if doc.file_size and doc.file_size > _DOC_MAX_BYTES:
        await msg.reply_text("fayl juda katta (20MB dan oshmasin).")
        return

    await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)

    try:
        f = await doc.get_file()
        data = bytes(await f.download_as_bytearray())
    except Exception:
        logger.exception("[dars] hujjatni yuklab bo'lmadi")
        await msg.reply_text("faylni ocholmadim, qaytadan yubor.")
        return

    prompt = caption or "Bu hujjatning qisqacha mazmunini ber va asosiy fikrlarni sanab o't."

    try:
        if mime == "text/plain":
            text = data.decode("utf-8", errors="ignore")[:_DOC_TEXT_LIMIT]
            full_prompt = f"{prompt}\n\n--- HUJJAT MATNI ---\n{text}"
            reply = await asyncio.to_thread(
                ai_core.get_ai_reply, str(user.id), full_prompt,
                name=user.first_name, source="telegram", extra_system=DOC_SYSTEM,
            )
        else:  # PDF yoki rasm-fayl — Gemini'ga to'g'ridan-to'g'ri (vision)
            reply = await asyncio.to_thread(
                ai_core.get_ai_reply, str(user.id), prompt,
                image_bytes=data, image_mime=mime or "application/pdf",
                name=user.first_name, source="telegram", extra_system=DOC_SYSTEM,
            )
    except Exception:
        logger.exception("[dars] hujjatni tahlil qilib bo'lmadi")
        await msg.reply_text("faylni o'qiy olmadim, keyinroq qayta ur.")
        return

    if not reply or reply == "...":
        reply = "bu fayldan hech narsa chiqmadi — boshqasini yubor yoki savolingni aniqroq yoz."

    await _send_long(msg, reply)

_STUDY_CARD = {
    "uz": (
        "📚 <b>Dars yordamchisi</b>",
        "Istalgan fandan savol yoki masalani yuboring — to'liq, qadam-baqadam "
        "yechib beraman.\n\n"
        "• <code>/dars 2x + 5 = 17 tenglamani yech</code>\n"
        "• Masala yozilgan rasmga <b>reply</b> qilib <code>/dars</code> deb yozing\n"
        "• Yoki rasmni shunchaki yuboring — chiqqan tugmalardan "
        "<b>🧮 Masalani yech</b> ni bosing\n"
        "• PDF, .txt yoki rasm-fayl yuboring — mazmunini qisqacha aytib "
        "beraman yoki savolingizga javob beraman\n\n"
        "Matematika, fizika, kimyo, biologiya, tarix, adabiyot, grammatika — "
        "qaysi fan bo'lishidan qat'i nazar ishlaydi.",
    ),
    "ru": (
        "📚 <b>Помощник по учёбе</b>",
        "Отправьте вопрос или задачу по любому предмету — решу полностью, "
        "по шагам.\n\n"
        "• <code>/dars 2x + 5 = 17 реши уравнение</code>\n"
        "• Ответьте (<b>reply</b>) на фото с задачей командой <code>/dars</code>\n"
        "• Или просто отправьте фото — в меню нажмите "
        "<b>🧮 Решить задачу</b>\n"
        "• Отправьте PDF, .txt или фото-файл — перескажу суть или отвечу "
        "на ваш вопрос по нему\n\n"
        "Математика, физика, химия, биология, история, литература, "
        "грамматика — работает с любым предметом.",
    ),
    "en": (
        "📚 <b>Study helper</b>",
        "Send a question or problem from any subject — I'll solve it fully, "
        "step by step.\n\n"
        "• <code>/dars solve 2x + 5 = 17</code>\n"
        "• Reply to a photo of the problem with <code>/dars</code>\n"
        "• Or just send the photo — tap <b>🧮 Solve the problem</b> in the menu\n"
        "• Send a PDF, .txt or image file — I'll summarize it or answer your "
        "question about it\n\n"
        "Math, physics, chemistry, biology, history, literature, grammar — "
        "works for any subject.",
    ),
}

_CHUNK_LIMIT = 3500


def _split_chunks(text: str, limit: int = _CHUNK_LIMIT) -> list[str]:
    """Uzun javobni Telegram xabar chegarasiga (4096) sig'adigan bo'laklarga
    ajratadi — imkon qadar bo'sh qatordan, bo'lmasa qatordan kesadi."""
    text = text.strip()
    if len(text) <= limit:
        return [text]
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit * 0.4:
            cut = text.rfind("\n", 0, limit)
        if cut < limit * 0.4:
            cut = limit
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


async def _send_long(message, text: str) -> None:
    """send_ai_reply bilan bir xil premium-emoji-xavfsiz yo'l, lekin 4096
    belgidan uzun javoblarni bir necha xabarga bo'lib yuboradi."""
    chunks = _split_chunks(text)
    total = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        body = f"({i}/{total})\n\n{chunk}" if total > 1 else chunk
        if extras.PLAIN_EMOJI_LIMIT:
            body = extras.drop_plain_emojis(body)
        rich = extras.premiumize(body)
        try:
            if rich:
                await message.reply_text(rich, parse_mode="HTML")
            else:
                await message.reply_text(body)
        except Exception:
            await message.reply_text(chunk)


# ═══════════════════════════════════════════════════════════════════════════
# 📎 /dars — matnli yoki rasmga reply qilingan savol
# ═══════════════════════════════════════════════════════════════════════════

async def cmd_dars(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not user or not chat:
        return
    if not extras._gate(update):
        return

    parts = (msg.text or "").split(maxsplit=1)
    question = parts[1].strip() if len(parts) > 1 else ""

    replied = msg.reply_to_message
    photo_msg = replied if (replied and replied.photo) else None

    if not question and not photo_msg:
        await msg.reply_text(USAGE, parse_mode="HTML")
        return

    await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)

    try:
        if photo_msg:
            f = await photo_msg.photo[-1].get_file()
            image_bytes = bytes(await f.download_as_bytearray())
            prompt = question or "Rasmdagi savol yoki masalani yech."
            reply = await asyncio.to_thread(
                ai_core.get_ai_reply, str(user.id), prompt,
                image_bytes=image_bytes, image_mime="image/jpeg",
                name=user.first_name, source="telegram",
                extra_system=STUDY_SYSTEM_PHOTO,
            )
        else:
            reply = await asyncio.to_thread(
                ai_core.get_ai_reply, str(user.id), question,
                name=user.first_name, source="telegram",
                extra_system=STUDY_SYSTEM,
            )
    except Exception:
        logger.exception("[dars] yechishda xato")
        await msg.reply_text("hozir yecholmadim, keyinroq qayta ur.")
        return

    if not reply or reply == "...":
        reply = "bu savolni ochib bera olmadim — aniqroq yoz yoki tiniqroq rasm yubor."

    await _send_long(msg, reply)


# ═══════════════════════════════════════════════════════════════════════════
# 🏠 /start menyusiga "Dars" tugmasi (extras._start_kb ni kichik patch qiladi)
# ═══════════════════════════════════════════════════════════════════════════

async def study_home_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = query.from_user
    lang = extras._get_lang(user.id) if user else "uz"
    title, body = _STUDY_CARD.get(lang, _STUDY_CARD["uz"])
    text = f"{title}\n{body}"

    def _kb(premium: bool) -> InlineKeyboardMarkup:
        mk = extras.icon_btn if premium else extras.plain_btn
        return InlineKeyboardMarkup([[mk(extras.tr(lang, "back"), "⬅️", callback_data="st:home")]])

    await extras.edit_html(query, text, _kb)


# ═══════════════════════════════════════════════════════════════════════════
# 🖼 Rasm menyusiga "🧮 Masalani yech" tugmasi (extras._photo_cache'ni ishlatadi)
# ═══════════════════════════════════════════════════════════════════════════

async def study_photo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[1] != "ph":
        await query.answer()
        return
    key = parts[2]
    file_id = extras._photo_cache.get(key)
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

    await query.answer("yechyapman...")
    await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
    try:
        f = await context.bot.get_file(file_id)
        data = bytes(await f.download_as_bytearray())
        reply = await asyncio.to_thread(
            ai_core.get_ai_reply, str(user.id), "Rasmdagi savol yoki masalani yech.",
            image_bytes=data, image_mime="image/jpeg",
            name=user.first_name, source="telegram",
            extra_system=STUDY_SYSTEM_PHOTO,
        )
    except Exception:
        logger.exception("[dars] rasm orqali yecholmadi")
        await query.message.reply_text("rasmni ko'ra olmadim, keyinroq ur.")
        return

    if not reply or reply == "...":
        reply = "bu masalani ochib bera olmadim — tiniqroq rasm yubor."

    await _send_long(query.message, reply)


def _install_patches() -> None:
    """Boshqa fayllarga tegmasdan: rasm menyusiga va /start menyusiga bitta-
    bittadan tugma qo'shadi (extras.py o'zi ham shu uslubda ishlaydi)."""
    global _patched
    if _patched:
        return
    _patched = True

    # 1) Rasm menyusi: "Tushuntir / Roast / Anime / Matn" qatoridan keyin
    #    "🧮 Masalani yech" + "Stiker yasash" qatori.
    _orig_photo_menu_kb = extras._photo_menu_kb

    def _photo_menu_kb_with_study(key: str, premium: bool) -> InlineKeyboardMarkup:
        mk = extras.icon_btn if premium else extras.plain_btn
        items = [
            mk(label, ch, callback_data=f"ph:{mode}:{key}")
            for mode, (ch, label, _p, _s) in extras._PHOTO_MODES.items()
        ]
        study_btn = mk("Masalani yech", "🧮", callback_data=f"study:ph:{key}")
        sticker_btn = mk("Stiker yasash", "🧩", callback_data=f"sk:photo:{key}")
        return InlineKeyboardMarkup([items[:2], items[2:], [study_btn, sticker_btn]])

    extras._photo_menu_kb = _photo_menu_kb_with_study

    # 2) /start bosh sahifasi: "add_group" / "admin" qatoridan oldin
    #    "📚 Dars" qatori.
    _orig_start_kb = extras._start_kb

    def _start_kb_with_study(chat_id, bot_username, premium, lang="uz") -> InlineKeyboardMarkup:
        kb = _orig_start_kb(chat_id, bot_username, premium, lang)
        mk = extras.icon_btn if premium else extras.plain_btn
        title = {"uz": "Dars", "ru": "Учёба", "en": "Study"}.get(lang, "Dars")
        study_row = [mk(title, "📚", callback_data="study:home")]

        rows = list(kb.inline_keyboard)
        insert_at = len(rows)
        for i, row in enumerate(rows):
            if any(getattr(btn, "url", None) for btn in row):
                insert_at = i
                break
        rows.insert(insert_at, study_row)
        return InlineKeyboardMarkup(rows)

    extras._start_kb = _start_kb_with_study


def register(app: Application) -> None:
    app.add_handler(CommandHandler(["dars", "study", "masala"], cmd_dars), group=0)
    app.add_handler(CallbackQueryHandler(study_home_callback, pattern=r"^study:home$"), group=0)
    app.add_handler(CallbackQueryHandler(study_photo_callback, pattern=r"^study:ph:"), group=0)
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document), group=0)
    _install_patches()
