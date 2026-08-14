"""
Misumi AI — shared core logic.

Used by both:
  - bot.py     (Telegram chat handlers)
  - webapp.py  (Telegram Mini App / web chat interface)

Provider chain for plain text messages:
  1. Cerebras  (primary  — fast, strong open-weight models, generous free tier)
  2. Gemini    (fallback — also handles image input, since Cerebras/Groq here
                are text-only)
  3. Groq      (final fallback)

If the user attaches an image, Gemini is used directly (only provider here
that accepts vision input); if that fails, no fallback exists for images.

Long-term memory works the same way across all three providers: instead of
provider-specific function/tool calling, the system prompt asks the model
to append an invisible tag like ⟦MEMORY:category:key:value⟧ at the end of
its reply when it learns something worth remembering. We strip those tags
before showing the reply and save them to memory_manager.
"""
import os
import re

import requests
from google import genai
from google.genai import types

import admin_store
import memory_manager as mem

BOT_NAME = "Misumi AI"
AUTHOR_HANDLE = "@QahramonovK"

MAX_HISTORY_TURNS = 30  # short-term context kept in RAM, per user

# ---------------------------------------------------------------------------
# Provider 1 (primary): Cerebras — https://cloud.cerebras.ai
# ---------------------------------------------------------------------------
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY")
CEREBRAS_MODEL = os.environ.get("CEREBRAS_MODEL", "llama-4-scout")
CEREBRAS_URL = "https://api.cerebras.ai/v1/chat/completions"

# ---------------------------------------------------------------------------
# Provider 2 (fallback + vision): Gemini — https://aistudio.google.com
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
_gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# Image generation — "Nano Banana". Imagen models are being retired
# (shutdown Aug 17, 2026), so this uses generate_content with an
# IMAGE response modality instead of the older generate_images API.
GEMINI_IMAGE_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")

# ---------------------------------------------------------------------------
# Provider 3 (final fallback): Groq — https://console.groq.com
# ---------------------------------------------------------------------------
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

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
future plans — silently remember it by appending, at the very end of your
reply (after your normal answer, on new lines, invisible to the user),
one tag per fact in this EXACT format:
⟦MEMORY:category:key:value⟧
category is one of: identity, preferences, projects, relationships,
wishes, notes. key is a short snake_case key (e.g. name, favorite_food).
value must be written in English, concise. Do NOT mention these tags to
the user, do NOT wrap them in code blocks, just append them silently.
Do NOT tag one-off requests or small talk — only durable facts.
"""

MEMORY_TAG_RE = re.compile(r"⟦MEMORY:([a-zA-Z_]+):([a-zA-Z0-9_]+):([^⟧]*)⟧")

# Keyword-based detection for "draw me / generate an image of ..." requests,
# across Uzbek, Russian, and English phrasing. Kept simple and explicit
# (like the YouTube-link intent check) rather than relying on the chat
# model to decide, so we never silently skip a real image request.
IMAGE_REQUEST_RE = re.compile(
    r"\b("
    r"rasm(?:ini|ni)?\s*(chiz|yasa|chizib|yasab)|"
    r"surat(?:ini|ni)?\s*(chiz|yasa|chizib|yasab)|"
    r"rasm\s*yarat|surat\s*yarat|"
    r"нарисуй|нарисуйте|сгенерируй\s*(изображение|картинку)|"
    r"generate\s+(an?\s+)?image|draw\s+(me\s+)?(a|an)\b|create\s+(an?\s+)?image"
    r")",
    re.IGNORECASE,
)


def wants_image(text: str) -> bool:
    return bool(text) and bool(IMAGE_REQUEST_RE.search(text))


def _call_gemini_image(prompt: str) -> tuple[bytes, str]:
    """Generate an image with Gemini ("Nano Banana"). Returns (bytes, mime_type).
    Raises if no image came back (e.g. blocked by safety filters)."""
    response = _gemini_client.models.generate_content(
        model=GEMINI_IMAGE_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
    )
    for part in response.parts:
        if getattr(part, "inline_data", None) and part.inline_data.data:
            return part.inline_data.data, part.inline_data.mime_type or "image/png"
    raise RuntimeError("Gemini rasm qaytarmadi (ehtimol xavfsizlik filtri to'sdi)")

# In-memory short-term conversation history per user (lost on restart —
# only long-term facts persist via memory_manager on disk). Shared between
# the Telegram chat and the Mini App since both key off the same user id.
# Format: list of {"role": "user"|"assistant", "content": str}
_history: dict[str, list] = {}


def _extract_memory_tags(text: str) -> tuple[str, list[tuple[str, str, str]]]:
    matches = MEMORY_TAG_RE.findall(text or "")
    clean = MEMORY_TAG_RE.sub("", text or "").strip()
    return clean, matches


def _call_cerebras(system_instruction: str, history: list, user_text: str) -> str:
    if not CEREBRAS_API_KEY:
        raise RuntimeError("CEREBRAS_API_KEY not set")
    messages = [{"role": "system", "content": system_instruction}]
    messages += [{"role": t["role"], "content": t["content"]} for t in history]
    messages.append({"role": "user", "content": user_text})
    resp = requests.post(
        CEREBRAS_URL,
        headers={
            "Authorization": f"Bearer {CEREBRAS_API_KEY}",
            "Content-Type": "application/json",
        },
        json={"model": CEREBRAS_MODEL, "messages": messages, "temperature": 0.8},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


def _call_groq(system_instruction: str, history: list, user_text: str) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY not set")
    messages = [{"role": "system", "content": system_instruction}]
    messages += [{"role": t["role"], "content": t["content"]} for t in history]
    messages.append({"role": "user", "content": user_text})
    resp = requests.post(
        GROQ_URL,
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        },
        json={"model": GROQ_MODEL, "messages": messages, "temperature": 0.8},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


def _call_gemini(
    system_instruction: str,
    history: list,
    user_text: str,
    image_bytes: bytes | None = None,
    image_mime: str | None = None,
) -> str:
    contents = []
    for turn in history:
        role = "user" if turn["role"] == "user" else "model"
        contents.append(types.Content(role=role, parts=[types.Part.from_text(text=turn["content"])]))

    parts = []
    if image_bytes:
        parts.append(types.Part.from_bytes(data=image_bytes, mime_type=image_mime or "image/jpeg"))
    parts.append(types.Part.from_text(text=user_text or "Rasmda nima bor?"))
    contents.append(types.Content(role="user", parts=parts))

    response = _gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(system_instruction=system_instruction),
    )
    return (response.text or "").strip()


def get_ai_reply(
    user_id: str,
    user_text: str,
    image_bytes: bytes | None = None,
    image_mime: str | None = None,
    name: str | None = None,
    source: str = "telegram",
) -> str:
    """Send a message as the given user and return Misumi AI's reply text.

    Shared by the Telegram bot handler and the Mini App's /api/chat route.
    If image_bytes is given, only Gemini (vision-capable) handles it.
    Otherwise tries Cerebras -> Gemini -> Groq in order, returning the
    first successful reply.

    `name` and `source` are optional metadata (display name, "telegram" or
    "miniapp") recorded for the admin panel's stats/user list.
    """
    user_id = str(user_id)
    admin_store.record_message(user_id, name=name, source=source)

    memory = mem.load_memory(user_id)
    memory_block = mem.format_memory_for_prompt(memory)
    system_instruction = SYSTEM_PROMPT + ("\n\n" + memory_block if memory_block else "")
    history = _history.get(user_id, [])

    raw_reply = None
    last_error: Exception | None = None

    if image_bytes:
        try:
            raw_reply = _call_gemini(system_instruction, history, user_text, image_bytes, image_mime)
        except Exception as e:
            last_error = e
    else:
        for call in (_call_cerebras, _call_gemini, _call_groq):
            try:
                raw_reply = call(system_instruction, history, user_text)
                break
            except Exception as e:
                print(f"[{call.__name__}] failed: {e}")
                last_error = e
                continue

    if raw_reply is None:
        raise last_error or RuntimeError("All AI providers failed")

    clean_text, tags = _extract_memory_tags(raw_reply)
    for category, key, value in tags:
        value = value.strip()
        if key and value:
            mem.update_memory(user_id, {category: {key: {"value": value}}})

    reply_text = clean_text.strip() or "..."

    history.append({"role": "user", "content": user_text or "[rasm]"})
    history.append({"role": "assistant", "content": reply_text})
    _history[user_id] = history[-MAX_HISTORY_TURNS:]

    return reply_text


def generate_image_reply(
    user_id: str,
    prompt: str,
    name: str | None = None,
    source: str = "telegram",
) -> tuple[bytes, str]:
    """Generate an image for the given prompt and record it in the user's
    short-term history (as a text placeholder, since we don't replay raw
    image bytes back into the chat context). Returns (image_bytes, mime_type).
    Raises on failure — caller should catch and show a friendly error.
    """
    user_id = str(user_id)
    admin_store.record_message(user_id, name=name, source=source)

    image_bytes, mime_type = _call_gemini_image(prompt)

    history = _history.get(user_id, [])
    history.append({"role": "user", "content": prompt})
    history.append({"role": "assistant", "content": "[rasm yaratib berdim]"})
    _history[user_id] = history[-MAX_HISTORY_TURNS:]

    return image_bytes, mime_type


def reset_user(user_id: str) -> None:
    user_id = str(user_id)
    mem.clear_memory(user_id)
    _history.pop(user_id, None)
