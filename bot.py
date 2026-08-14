"""
Jarvis Telegram Bot
--------------------
A pure chat/AI-assistant Telegram bot powered by Google Gemini, carrying
over Jarvis's personality and long-term memory from the original Jarvis
MK37 desktop project — with NO system/computer-control capabilities.

Env vars required (see .env.example):
  TELEGRAM_BOT_TOKEN   - from @BotFather
  GEMINI_API_KEY       - from https://aistudio.google.com/apikey
  ALLOWED_CHAT_IDS     - optional, comma-separated chat_ids. If set, only
                          these chats can use the bot (recommended if you
                          don't want strangers finding your bot).
  MEMORY_DIR           - optional, defaults to ./memory (see memory_manager.py)
  GEMINI_MODEL         - optional, defaults to "gemini-2.5-flash"
"""
import logging
import os

from google import genai
from google.genai import types
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import memory_manager as mem

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("jarvis-bot")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
ALLOWED_CHAT_IDS = {
    c.strip() for c in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if c.strip()
}
MAX_HISTORY_TURNS = 30  # short-term context kept in RAM, per chat

client = genai.Client(api_key=GEMINI_API_KEY)

SYSTEM_PROMPT = """You are JARVIS — an efficient, professional, direct AI assistant.
No fluff, no filler. Reply in 1-3 short sentences unless the user clearly
needs more detail (explanations, code, lists, etc).
Always respond in the same language the user is writing in.

Whenever the user reveals something worth remembering long-term — their
name, age, city, job, preferences, hobbies, relationships, projects, or
future plans — silently call save_memory. Never announce that you are
saving something, just call it. Do NOT save one-off requests or small talk.
Memory values must be written in English regardless of the conversation
language.
"""

SAVE_MEMORY_DECLARATION = types.FunctionDeclaration(
    name="save_memory",
    description=(
        "Save an important personal fact about the user to long-term memory. "
        "Call this silently whenever the user reveals something worth "
        "remembering: name, age, city, job, preferences, hobbies, "
        "relationships, projects, or future plans. Do NOT call for "
        "one-time questions or small talk."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {
            "category": {
                "type": "STRING",
                "description": (
                    "identity — name, age, birthday, city, job, language, "
                    "nationality | preferences — favorite food/color/music/"
                    "film/game/sport, hobbies | projects — active projects, "
                    "goals, things being built | relationships — friends, "
                    "family, partner, colleagues | wishes — future plans, "
                    "things to buy, travel dreams | notes — anything else "
                    "worth remembering"
                ),
            },
            "key": {
                "type": "STRING",
                "description": "Short snake_case key (e.g. name, favorite_food)",
            },
            "value": {
                "type": "STRING",
                "description": "Concise value in English (e.g. Fatih, pizza)",
            },
        },
        "required": ["category", "key", "value"],
    },
)

# In-memory short-term conversation history per chat (lost on restart —
# only long-term facts persist via memory_manager on disk).
_history: dict[str, list] = {}


def _authorized(chat_id) -> bool:
    return not ALLOWED_CHAT_IDS or str(chat_id) in ALLOWED_CHAT_IDS


def _build_chat(chat_id: str):
    memory = mem.load_memory(chat_id)
    memory_block = mem.format_memory_for_prompt(memory)
    system_instruction = SYSTEM_PROMPT + ("\n\n" + memory_block if memory_block else "")
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        tools=[types.Tool(function_declarations=[SAVE_MEMORY_DECLARATION])],
    )
    history = _history.get(chat_id, [])
    return client.chats.create(model=GEMINI_MODEL, config=config, history=history)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _authorized(chat_id):
        await update.message.reply_text("Sorry, this bot is private.")
        return
    await update.message.reply_text(
        "Jarvis online. Yozing — suhbatlashamiz. /reset — xotirani tozalash."
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _authorized(chat_id):
        return
    mem.clear_memory(str(chat_id))
    _history.pop(str(chat_id), None)
    await update.message.reply_text("Xotira tozalandi.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if not _authorized(chat_id):
        return
    user_text = update.message.text
    if not user_text:
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    chat = _build_chat(chat_id)

    try:
        response = chat.send_message(user_text)

        # Handle any function calls (save_memory) the model made.
        function_calls = response.function_calls or []
        for fn in function_calls:
            if fn.name != "save_memory":
                continue
            args = dict(fn.args)
            category = args.get("category", "notes")
            key = args.get("key")
            value = args.get("value")
            if key and value:
                mem.update_memory(chat_id, {category: {key: {"value": value}}})
            response = chat.send_message(
                types.Part.from_function_response(
                    name="save_memory", response={"result": "ok"}
                )
            )

        reply_text = (response.text or "...").strip()
    except Exception:
        logger.exception("Gemini error")
        reply_text = "Kechirasiz, xatolik yuz berdi. Birozdan so'ng qayta urinib ko'ring."

    _history[chat_id] = chat.get_history()[-MAX_HISTORY_TURNS:]

    await update.message.reply_text(reply_text)


def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Jarvis Telegram bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
