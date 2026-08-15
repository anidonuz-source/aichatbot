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
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import admin_store
import ai_core
import game
import sticker_store
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
    if not WEBAPP_URL:
        return None
    rows = [[InlineKeyboardButton(f"✦ {ai_core.BOT_NAME} ni ochish", web_app=WebAppInfo(url=WEBAPP_URL))]]
    if _is_admin(chat_id):
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info(f"{ai_core.BOT_NAME} Telegram bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
