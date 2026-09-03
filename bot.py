"""
Misumi AI — Telegram Bot
------------------------
Text chat in Telegram + a button that opens the Misumi AI Mini App
(a premium web chat interface, see webapp.py + templates/index.html).
Both surfaces share persona, memory, and conversation logic via ai_core.py.

Env vars required (see .env.example):
  TELEGRAM_BOT_TOKEN   - from @BotFather
  GEMINI_API_KEY       - from https://aistudio.google.com/apikey
  ALLOWED_CHAT_IDS     - optional, comma-separated chat_ids. If set, only
                          these chats can use the bot.
  MEMORY_DIR           - optional, defaults to ./memory (see memory_manager.py)
  GEMINI_MODEL         - optional, defaults to "gemini-3.6-flash"
  WEBAPP_URL           - the public HTTPS URL of this service (Render sets
                          RENDER_EXTERNAL_URL automatically — used as a
                          fallback if WEBAPP_URL isn't set).
"""
import asyncio
import logging
import os
import threading

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telethon.errors import SessionPasswordNeededError

import admin_store
import ai_core
import game
import ship
import sticker_store
import userbot_manager
import userbot_store
import webapp

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("misumi-bot")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_CHAT_IDS = {
    c.strip() for c in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if c.strip()
}
WEBAPP_URL = os.environ.get("WEBAPP_URL") or os.environ.get("RENDER_EXTERNAL_URL")
ADMIN_ID = os.environ.get("ADMIN_ID", "").strip()


def _authorized(chat_id) -> bool:
    return not ALLOWED_CHAT_IDS or str(chat_id) in ALLOWED_CHAT_IDS


def _is_admin(chat_id) -> bool:
    return bool(ADMIN_ID) and str(chat_id) == ADMIN_ID


def _webapp_keyboard(chat_id=None) -> InlineKeyboardMarkup | None:
    rows = []
    if WEBAPP_URL:
        rows.append([InlineKeyboardButton(f"✦ {ai_core.BOT_NAME} ni ochish", web_app=WebAppInfo(url=WEBAPP_URL))])
    rows.append([InlineKeyboardButton("👤 Hisob", callback_data="ub:account")])
    if WEBAPP_URL and _is_admin(chat_id):
        admin_url = f"{WEBAPP_URL.rstrip('/')}/admin"
        rows.append([InlineKeyboardButton("⚙️ Admin panel", web_app=WebAppInfo(url=admin_url))])
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _authorized(chat_id):
        await update.message.reply_text("Sorry, this bot is private.")
        return
    first_name = update.effective_user.first_name if update.effective_user else None
    greeting = f"Assalomu alaykum, {first_name}." if first_name else "Assalomu alaykum."
    text = (
        f"✦ {greeting} Men {ai_core.BOT_NAME} — shaxsiy AI hamrohingiz.\n\n"
        f"Yozing — suhbatlashamiz, savol bering, fikr almashing. Men gaplaringizni "
        f"eslab qolaman, shuning uchun har safar bir joydan davom etaman.\n\n"
        f"Premium interfeys uchun quyidagi tugmani bosing — rasm yuborish, "
        f"tarixni saqlash va yanada boy tajriba shu yerda.\n\n"
        f"/reset — xotirani tozalash\n"
        f"/duel — birovga (yoki menga) o'yin taklif qilish 🎲\n"
        f"/ship — guruhdan tasodifiy juftlik tanlash 💘\n"
        f"/reyting — o'yin reytingi\n"
        f"/stiker — stikerga reply qilib, uni aniq turkumga qo'shish "
        f"(qolganini o'zim guruhda ko'rganimcha yig'ib olaman)\n\n"
        f"Yaratuvchi: {ai_core.AUTHOR_HANDLE}"
    )
    await update.message.reply_text(text, reply_markup=_webapp_keyboard(chat_id))


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _authorized(chat_id):
        return
    ai_core.reset_user(chat_id)
    await update.message.reply_text("Xotira tozalandi.")


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /broadcast <matn> sends that text to every chat Misumi
    has ever talked in (private chats and groups alike). Skips chats
    where sending fails (bot blocked/removed, chat deleted, etc.) instead
    of aborting the whole run, and reports how many succeeded/failed."""
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        return

    text = update.message.text.split(maxsplit=1)
    if len(text) < 2 or not text[1].strip():
        await update.message.reply_text("Foydalanish: /broadcast <xabar matni>")
        return
    broadcast_text = text[1].strip()

    targets = admin_store.get_all_chat_ids()
    if not targets:
        await update.message.reply_text("Hali hech qanday chat qayd etilmagan.")
        return

    await update.message.reply_text(f"📣 {len(targets)} ta chatga yuborilyapti...")

    sent, failed = 0, 0
    for target_id in targets:
        try:
            await context.bot.send_message(chat_id=int(target_id), text=broadcast_text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # stay well under Telegram's rate limits

    await update.message.reply_text(f"✅ Yuborildi: {sent}\n❌ Yetmadi: {failed}")


# ---------------------------------------------------------------------------
# "Hisob" — userbot connect/manage flow (Misumi AI Pro feature)
# ---------------------------------------------------------------------------

UB_PHONE, UB_CODE, UB_PASSWORD = range(3)


def _account_menu_text_and_kb(user_id) -> tuple[str, InlineKeyboardMarkup]:
    if not userbot_store.is_connected(user_id):
        text = (
            "👤 <b>Hisobingiz</b>\n\n"
            "Shaxsiy Telegram akkountingizni Misumi AI'ga ulang — siz oflayn "
            "bo'lganingizda AI sizning o'rningizga tabiiy javob yozadi va "
            "bio'ingizni holatga qarab yangilab turadi."
        )
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔗 AI ga akkount ulash", callback_data="ub:connect")]]
        )
        return text, kb

    meta = userbot_store.get_account_meta(user_id) or {}
    phone = meta.get("phone", "—")
    pro = admin_store.is_premium(user_id)
    text = (
        "👤 <b>Hisobingiz</b>\n\n"
        f"📱 Ulangan raqam: <code>{phone}</code>\n"
        f"💎 Holat: {'Misumi AI Pro' if pro else 'Oddiy (Pro emas)'}"
    )
    if pro and userbot_store.get_settings(user_id).get("inner_ai"):
        text += "\n\n💬 Saqlangan xabarlarga <code>.ai savolingiz</code> deb yozib AI bilan gaplasha olasiz."
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Hisob statistikasi", callback_data="ub:stats")],
            [InlineKeyboardButton("⚙️ Xizmatlar", callback_data="ub:services")],
            [InlineKeyboardButton("🔌 Akkountni uzish", callback_data="ub:disconnect")],
        ]
    )
    return text, kb


def _services_menu_text_and_kb(user_id) -> tuple[str, InlineKeyboardMarkup]:
    pro = admin_store.is_premium(user_id)
    settings = userbot_store.get_settings(user_id)

    def _mark(v):
        return "🟢 Yoqilgan" if v else "🔴 O'chirilgan"

    lines = [
        "⚙️ <b>Xizmatlar</b>\n",
        f"🤖 Oflayn avto-javob (bepul): {_mark(settings.get('auto_reply'))}",
    ]
    kb_rows = [
        [InlineKeyboardButton(
            f"{'🔴 O\u2019chirish' if settings.get('auto_reply') else '🟢 Yoqish'} — Avto-javob",
            callback_data="ub:toggle:auto_reply",
        )],
    ]

    if settings.get("auto_reply"):
        sig_note = (
            " (birinchi xabarga qo'shiladi)" if not pro else
            " — o'chirish mumkin, pastda"
        )
        lines.append(f"✉️ Oflayn bildirishnoma imzosi{sig_note}: {_mark(settings.get('signature', True))}")

    if pro:
        offline_min = settings.get("offline_minutes") or userbot_manager.DEFAULT_OFFLINE_MINUTES
        lines.append(f"📝 Avto-bio (Pro): {_mark(settings.get('auto_bio'))}")
        lines.append(f"🧠 Ichki AI yordamchisi (Pro): {_mark(settings.get('inner_ai'))}")
        lines.append(f"🎙 Ovozli xabarga javob (Pro): {_mark(settings.get('voice_reply'))}")
        lines.append(f"🕐 Oflayn hisoblanish vaqti (Pro): {offline_min} daqiqa")
        n_kw = len(settings.get("keywords", {}))
        n_bl = len(settings.get("blacklist", []))
        digest = settings.get("stats_digest")
        lines.append(f"🔑 Kalit so'zlar (Pro): {n_kw} ta sozlangan")
        lines.append(f"🚫 Qora ro'yxat (Pro): {n_bl} kishi")
        lines.append(f"📊 Statistika hisoboti (Pro): {digest or 'o\u2019chirilgan'}")
        kb_rows.append([InlineKeyboardButton(
            f"{'🔴 O\u2019chirish' if settings.get('auto_bio') else '🟢 Yoqish'} — Avto-bio",
            callback_data="ub:toggle:auto_bio",
        )])
        kb_rows.append([InlineKeyboardButton(
            f"{'🔴 O\u2019chirish' if settings.get('inner_ai') else '🟢 Yoqish'} — Ichki AI yordamchisi",
            callback_data="ub:toggle:inner_ai",
        )])
        kb_rows.append([InlineKeyboardButton(
            f"{'🔴 O\u2019chirish' if settings.get('voice_reply') else '🟢 Yoqish'} — Ovozli xabarga javob",
            callback_data="ub:toggle:voice_reply",
        )])
        if settings.get("auto_reply"):
            kb_rows.append([InlineKeyboardButton(
                f"{'🔴 O\u2019chirish' if settings.get('signature', True) else '🟢 Yoqish'} — Oflayn imzo",
                callback_data="ub:toggle:signature",
            )])
        kb_rows.append([InlineKeyboardButton("🕐 Oflayn vaqtini sozlash", callback_data="ub:schedule")])
        lines.append(
            "\n<i>Kalit so'z, qora ro'yxat va statistika hisobotini sozlash uchun "
            "ulangan akkountingizda Saqlangan xabarlarga yozing:</i>\n"
            "<code>.kw narx | 1 oylik obuna narxi 50 000 so'm</code>\n"
            "<code>.block @username</code> / <code>.unblock @username</code>\n"
            "<code>.stats daily</code> / <code>.stats weekly</code> / <code>.stats off</code>"
        )
    else:
        lines.append("\n🔒 <b>Misumi AI Pro</b> bilan yana ko'proq narsa ochiladi:")
        lines.append("• 📝 Avtomatik bio yangilanishi")
        lines.append("• 🧠 Ichki AI yordamchisi — Saqlangan xabarlarga <code>.ai savolingiz</code> deb yozib AI bilan bevosita gaplashish")
        lines.append("• 🎙 Ovozli xabarlarni tinglab, o'rniga javob yozish")
        lines.append("• 🔑 Kalit so'zga tayyor javob (masalan \"narx\" desa avtomatik javob)")
        lines.append("• 🚫 Tanlangan odamlarga avto-javob yubormaslik (qora ro'yxat)")
        lines.append("• 📊 Kunlik/haftalik statistika hisoboti Saqlangan xabarlarga")
        lines.append("• 🕐 Oflayn hisoblanish vaqtini o'zingiz belgilash")
        lines.append("• ✉️ Oflayn imzo yozuvini o'chirish (bepulda har doim ko'rinadi)")
        kb_rows.append([InlineKeyboardButton("💎 Misumi AI Pro sotib olish", callback_data="ub:buy_pro")])

    kb_rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="ub:account")])
    return "\n".join(lines), InlineKeyboardMarkup(kb_rows)


def _schedule_menu_text_and_kb(user_id) -> tuple[str, InlineKeyboardMarkup]:
    settings = userbot_store.get_settings(user_id)
    current = settings.get("offline_minutes") or userbot_manager.DEFAULT_OFFLINE_MINUTES
    text = (
        "🕐 <b>Oflayn hisoblanish vaqti</b>\n\n"
        "Akkountingizda necha daqiqa harakat bo'lmasa, sizni 'oflayn' deb "
        "hisoblab AI javob yoza boshlasin?"
    )
    options = [1, 3, 5, 10, 15, 30]
    rows = []
    row = []
    for m in options:
        label = f"{'✅ ' if m == current else ''}{m} daqiqa"
        row.append(InlineKeyboardButton(label, callback_data=f"ub:setmin:{m}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="ub:services")])
    return text, InlineKeyboardMarkup(rows)


async def ub_callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    data = query.data

    if data == "ub:account":
        await query.answer()
        text, kb = _account_menu_text_and_kb(user_id)
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

    elif data == "ub:stats":
        await query.answer()
        stats = userbot_store.get_stats(user_id)
        text = (
            "📊 <b>Hisob statistikasi</b>\n\n"
            f"🤖 Yuborilgan avto-javoblar: {stats.get('auto_replies_sent', 0)}\n"
            f"🔑 Kalit so'z javoblari: {stats.get('keyword_replies_sent', 0)}\n"
            f"🎙 Ovozli xabar javoblari: {stats.get('voice_replies_sent', 0)}\n"
            f"📝 Bio yangilanishlar: {stats.get('bio_updates', 0)}\n"
            f"🧠 Ichki AI suhbatlari: {stats.get('self_chat_replies', 0)}"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Orqaga", callback_data="ub:account")]])
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

    elif data == "ub:services":
        await query.answer()
        text, kb = _services_menu_text_and_kb(user_id)
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

    elif data.startswith("ub:toggle:"):
        key = data.split(":", 2)[2]
        pro_only_keys = {"auto_bio", "inner_ai", "signature", "voice_reply"}
        if key in pro_only_keys and not admin_store.is_premium(user_id):
            await query.answer("Bu funksiya faqat Misumi AI Pro uchun.", show_alert=True)
            return
        new_settings = userbot_store.set_setting(user_id, key, not userbot_store.get_settings(user_id).get(key))
        if new_settings.get(key):
            await userbot_manager.start_userbot(user_id)
        await query.answer("Yangilandi ✅")
        text, kb = _services_menu_text_and_kb(user_id)
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

    elif data == "ub:schedule":
        if not admin_store.is_premium(user_id):
            await query.answer("Bu funksiya faqat Misumi AI Pro uchun.", show_alert=True)
            return
        await query.answer()
        text, kb = _schedule_menu_text_and_kb(user_id)
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

    elif data.startswith("ub:setmin:"):
        if not admin_store.is_premium(user_id):
            await query.answer("Bu funksiya faqat Misumi AI Pro uchun.", show_alert=True)
            return
        minutes = int(data.split(":", 2)[2])
        userbot_store.set_setting(user_id, "offline_minutes", minutes)
        await query.answer(f"✅ {minutes} daqiqaga o'rnatildi")
        text, kb = _schedule_menu_text_and_kb(user_id)
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

    elif data == "ub:buy_pro":
        await query.answer("So'rov adminga yuborildi ✅", show_alert=True)
        if ADMIN_ID:
            requester = update.effective_user
            uname = f"@{requester.username}" if requester.username else requester.full_name
            admin_kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("✅ Tasdiqlash (Pro berish)", callback_data=f"ub:approve:{user_id}")]]
            )
            await context.bot.send_message(
                chat_id=int(ADMIN_ID),
                text=f"💎 Misumi AI Pro so'rovi\n\nFoydalanuvchi: {uname} (ID: {user_id})",
                reply_markup=admin_kb,
            )

    elif data.startswith("ub:approve:"):
        if not _is_admin(user_id):
            await query.answer("Ruxsat yo'q.", show_alert=True)
            return
        target_id = data.split(":", 2)[2]
        admin_store.toggle_premium(target_id)
        await query.answer("Tasdiqlandi ✅")
        await query.edit_message_text(f"✅ Foydalanuvchi {target_id} endi Misumi AI Pro.")
        try:
            await context.bot.send_message(
                chat_id=int(target_id),
                text="💎 Tabriklaymiz! Misumi AI Pro faollashtirildi. Endi Xizmatlar bo'limidagi funksiyalarni yoqishingiz mumkin.",
            )
        except Exception:
            pass

    elif data == "ub:disconnect":
        await query.answer()
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("❗️ Ha, uzish", callback_data="ub:disconnect_confirm")],
                [InlineKeyboardButton("⬅️ Bekor qilish", callback_data="ub:account")],
            ]
        )
        await query.edit_message_text("Akkountni Misumi AI'dan uzmoqchimisiz?", reply_markup=kb)

    elif data == "ub:disconnect_confirm":
        await query.answer()
        await userbot_manager.stop_userbot(user_id)
        userbot_store.disconnect(user_id)
        text, kb = _account_menu_text_and_kb(user_id)
        await query.edit_message_text("🔌 Akkount uzildi.\n\n" + text, reply_markup=kb, parse_mode="HTML")


async def ub_connect_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "📱 Telegram akkountingiz raqamini xalqaro formatda yuboring.\n"
        "Masalan: <code>+998901234567</code>\n\n/cancel — bekor qilish",
        parse_mode="HTML",
    )
    return UB_PHONE


async def ub_receive_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.text.strip()
    context.user_data["ub_phone"] = phone
    try:
        await userbot_manager.start_login(update.effective_user.id, phone)
    except RuntimeError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return ConversationHandler.END
    except Exception:
        logger.exception("Userbot start_login error")
        await update.message.reply_text(
            "⚠️ Kod yuborishda xatolik. Raqamni tekshirib qayta urinib ko'ring yoki /cancel bosing."
        )
        return UB_PHONE
    await update.message.reply_text("💬 Telegramga kelgan kodni yuboring:\n\n/cancel — bekor qilish")
    return UB_CODE


async def ub_receive_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.message.text.strip()
    phone = context.user_data.get("ub_phone", "")
    try:
        session_string = await userbot_manager.submit_code(update.effective_user.id, phone, code)
    except SessionPasswordNeededError:
        await update.message.reply_text(
            "🔐 Ikki bosqichli tasdiqlash (2FA) yoqilgan. Parolingizni yuboring:\n\n/cancel — bekor qilish"
        )
        return UB_PASSWORD
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return UB_CODE
    except Exception:
        logger.exception("Userbot submit_code error")
        await update.message.reply_text("⚠️ Xatolik yuz berdi. /cancel bosib qayta urinib ko'ring.")
        return UB_CODE

    return await _finish_connect(update, context, phone, session_string)


async def ub_receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.message.text.strip()
    phone = context.user_data.get("ub_phone", "")
    try:
        session_string = await userbot_manager.submit_password(update.effective_user.id, password)
    except Exception:
        logger.exception("Userbot submit_password error")
        await update.message.reply_text("⚠️ Parol noto'g'ri yoki xatolik yuz berdi. /cancel bosib qayta urinib ko'ring.")
        return UB_PASSWORD

    return await _finish_connect(update, context, phone, session_string)


async def _finish_connect(update: Update, context: ContextTypes.DEFAULT_TYPE, phone: str, session_string: str) -> int:
    user_id = update.effective_user.id
    userbot_store.save_session(user_id, phone, session_string)
    await userbot_manager.start_userbot(user_id)
    context.user_data.pop("ub_phone", None)
    text, kb = _account_menu_text_and_kb(user_id)
    await update.message.reply_text("✅ Akkount muvaffaqiyatli ulandi!\n\n" + text, reply_markup=kb, parse_mode="HTML")
    return ConversationHandler.END


async def ub_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    userbot_manager.cancel_login(update.effective_user.id)
    context.user_data.pop("ub_phone", None)
    await update.message.reply_text("Bekor qilindi.")
    return ConversationHandler.END


def _should_respond_in_group(update: Update, bot_username: str | None) -> bool:
    """In group chats, only respond when explicitly addressed: a reply to
    the bot's own message, an @mention of the bot, or the word 'misumi'
    (or 'misumi ai') anywhere in the text. Private chats always respond.
    """
    message = update.message
    if update.effective_chat.type == "private":
        return True

    # Reply to one of the bot's own messages.
    if message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.is_bot:
        if bot_username and message.reply_to_message.from_user.username == bot_username:
            return True

    text = (message.text or "").lower()

    if bot_username and f"@{bot_username.lower()}" in text:
        return True

    if "misumi ai" in text or "misumi" in text:
        return True

    return False


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _authorized(chat_id):
        return
    user_text = update.message.text
    if not user_text:
        return

    bot_username = context.bot.username
    if not _should_respond_in_group(update, bot_username):
        return

    if admin_store.is_blocked(chat_id):
        return

    if admin_store.is_maintenance() and not _is_admin(chat_id):
        await update.message.reply_text(
            f"🛠 {ai_core.BOT_NAME} hozir texnik ishlar tufayli vaqtincha ishlamayapti. "
            "Birozdan so'ng qayta urinib ko'ring."
        )
        return

    user = update.effective_user
    if user and not ai_core.check_rate_limit(chat_id, user.id):
        if ai_core.should_warn(chat_id, user.id):
            await update.message.reply_text("Biroz sekinroq yozing 🙂")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    display_name = user.first_name if user else None

    if ai_core.wants_image(user_text):
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
        try:
            image_bytes, _mime = ai_core.generate_image_reply(
                chat_id, user_text, name=display_name, source="telegram"
            )
        except Exception:
            logger.exception("Image generation error")
            await update.message.reply_text(
                "Kechirasiz, rasm yaratishda xatolik yuz berdi. Birozdan so'ng qayta urinib ko'ring."
            )
            return
        await update.message.reply_photo(photo=image_bytes)
        return

    try:
        reply_text = ai_core.get_ai_reply(chat_id, user_text, name=display_name, source="telegram")
    except Exception:
        logger.exception("Gemini error")
        reply_text = "Kechirasiz, xatolik yuz berdi. Birozdan so'ng qayta urinib ko'ring."

    await ai_core.deliver_ai_reply(
        context.bot,
        chat_id,
        chat_id,
        reply_text,
        reply_to_message_id=update.message.message_id,
    )


def _start_webapp_server():
    port = int(os.environ.get("PORT", "10000"))
    logger.info(f"Misumi AI web server listening on port {port}")
    webapp.run(port)


def main():
    # Render Web Services require a bound port to consider the service
    # healthy. This also happens to be our real Mini App server.
    threading.Thread(target=_start_webapp_server, daemon=True).start()

    if not WEBAPP_URL:
        logger.warning(
            "WEBAPP_URL / RENDER_EXTERNAL_URL not set — the Mini App button "
            "will be hidden. Set WEBAPP_URL to this service's public HTTPS URL."
        )

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    game.register(app)
    ship.register(app)

    ub_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ub_connect_entry, pattern="^ub:connect$")],
        states={
            UB_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ub_receive_phone)],
            UB_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ub_receive_code)],
            UB_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, ub_receive_password)],
        },
        fallbacks=[CommandHandler("cancel", ub_cancel)],
    )
    app.add_handler(ub_conv)
    app.add_handler(CallbackQueryHandler(ub_callback_router, pattern="^ub:"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def _post_init(_app):
        await userbot_manager.resume_all()

    app.post_init = _post_init

    logger.info(f"{ai_core.BOT_NAME} Telegram bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
