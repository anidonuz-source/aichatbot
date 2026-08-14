"""
Misumi AI — shared core logic.

Used by both:
  - bot.py     (Telegram chat handlers)
  - webapp.py  (Telegram Mini App / web chat interface)

Both surfaces share the same Gemini persona, long-term memory, and
save_memory tool, keyed by the user's Telegram id — so a conversation
continues seamlessly whether the person types in the bot chat or opens
the Mini App.
"""
import os

import requests
from google import genai
from google.genai import types

import memory_manager as mem

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
BOT_NAME = "Misumi AI"
AUTHOR_HANDLE = "@QahramonovK"

# Optional fallback: if Gemini fails (quota exhausted, model unavailable,
# etc.), and a Grok API key is set, Misumi silently retries the same
# message through xAI's Grok instead of showing an error.
GROK_API_KEY = os.environ.get("GROK_API_KEY")
GROK_MODEL = os.environ.get("GROK_MODEL", "grok-4.1-fast")
GROK_URL = "https://api.x.ai/v1/chat/completions"

MAX_HISTORY_TURNS = 30  # short-term context kept in RAM, per user

client = genai.Client(api_key=GEMINI_API_KEY)

SYSTEM_PROMPT = f"""You are {BOT_NAME} — a sleek, premium, highly capable AI
companion. Your tone is warm but polished: confident, concise, never
robotic, never generic. Think "luxury concierge who happens to be a
brilliant assistant" rather than a bare-bones chatbot.
Reply in 1-3 sentences unless the user clearly needs more detail
(explanations, code, structured lists, etc).
Always respond in the same language the user is writing in.
You were built by {AUTHOR_HANDLE}. If asked who made you, credit them
naturally — don't over-mention it otherwise.

If the user asks you to write or generate a large or complex piece of
code — a full script, an app, a bot, a multi-function program, or
anything that would take real effort to produce — do NOT write it.
Instead, warmly and briefly let them know that full code generation is
a premium feature, and that they can unlock it by upgrading to
{BOT_NAME} Pro — contact {AUTHOR_HANDLE} for that. Keep it short,
friendly, on-brand — never robotic or apologetic-sounding. Vary your
phrasing naturally instead of repeating the same sentence every time.
Small things are fine to answer directly: a one-liner, a short
snippet under ~10 lines, fixing a small bug, or explaining a concept
— only gate the big stuff.

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

# In-memory short-term conversation history per user (lost on restart —
# only long-term facts persist via memory_manager on disk). Shared between
# the Telegram chat and the Mini App since both key off the same user id.
_history: dict[str, list] = {}


def _build_chat(user_id: str):
    memory = mem.load_memory(user_id)
    memory_block = mem.format_memory_for_prompt(memory)
    system_instruction = SYSTEM_PROMPT + ("\n\n" + memory_block if memory_block else "")
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        tools=[types.Tool(function_declarations=[SAVE_MEMORY_DECLARATION])],
    )
    history = _history.get(user_id, [])
    return client.chats.create(model=GEMINI_MODEL, config=config, history=history)


def _call_grok(system_instruction: str, user_text: str) -> str | None:
    """Best-effort fallback through xAI's Grok. Returns None (never raises)
    if GROK_API_KEY isn't set or the call fails, so callers can just check
    for None and re-raise the original Gemini error.
    """
    if not GROK_API_KEY:
        return None
    try:
        resp = requests.post(
            GROK_URL,
            headers={
                "Authorization": f"Bearer {GROK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROK_MODEL,
                "messages": [
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": user_text},
                ],
                "temperature": 0.8,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[Grok fallback] failed: {e}")
        return None


def get_ai_reply(
    user_id: str,
    user_text: str,
    image_bytes: bytes | None = None,
    image_mime: str | None = None,
) -> str:
    """Send a message as the given user and return Misumi AI's reply text.

    Shared by the Telegram bot handler and the Mini App's /api/chat route.
    If image_bytes is given, it's attached to the message (e.g. a photo
    sent from the Mini App), and Misumi will look at it before replying.

    If Gemini fails (quota exhausted, model retired, etc.) and GROK_API_KEY
    is configured, silently falls back to Grok so the user never sees an
    error — though Grok mode doesn't support image input or memory saves.
    """
    user_id = str(user_id)
    memory = mem.load_memory(user_id)
    memory_block = mem.format_memory_for_prompt(memory)
    system_instruction = SYSTEM_PROMPT + ("\n\n" + memory_block if memory_block else "")

    try:
        chat = _build_chat(user_id)

        if image_bytes:
            parts = [
                types.Part.from_bytes(data=image_bytes, mime_type=image_mime or "image/jpeg"),
                types.Part.from_text(text=user_text or "Rasmda nima bor?"),
            ]
            response = chat.send_message(parts)
        else:
            response = chat.send_message(user_text)

        for fn in (response.function_calls or []):
            if fn.name != "save_memory":
                continue
            args = dict(fn.args)
            category = args.get("category", "notes")
            key = args.get("key")
            value = args.get("value")
            if key and value:
                mem.update_memory(user_id, {category: {key: {"value": value}}})
            response = chat.send_message(
                types.Part.from_function_response(name="save_memory", response={"result": "ok"})
            )

        reply_text = (response.text or "...").strip()
        _history[user_id] = chat.get_history()[-MAX_HISTORY_TURNS:]
        return reply_text

    except Exception as gemini_error:
        fallback = _call_grok(system_instruction, user_text or "Salom")
        if fallback:
            return fallback
        raise gemini_error


def reset_user(user_id: str) -> None:
    user_id = str(user_id)
    mem.clear_memory(user_id)
    _history.pop(user_id, None)
