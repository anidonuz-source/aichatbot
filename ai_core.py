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
import asyncio
import os
import random
import re
import time

import requests
from google import genai
from google.genai import types

import admin_store
import memory_manager as mem
import sticker_store

BOT_NAME = "Misumi AI"
AUTHOR_HANDLE = "@QahramonovK"
CHANNEL_HANDLE = "@MisumiAi"  # official Misumi AI Telegram channel

MAX_HISTORY_TURNS = 30  # short-term context kept in RAM, per user

# ---------------------------------------------------------------------------
# Model tiers — a Claude/ChatGPT-style model picker shown in the Mini App.
# These aren't separate underlying AI providers so much as separate
# *personas/permission levels* layered on top of the same Cerebras/Gemini/
# Groq chain: Flash is fast and free, Pro/Max unlock full code generation,
# longer answers, and (for Max) a quality-first provider order. Gating is
# enforced server-side via admin_store.is_premium, not just prompted.
# ---------------------------------------------------------------------------
MODEL_TIERS = {
    "flash": {
        "id": "flash",
        "label": f"{BOT_NAME} Flash",
        "tagline": "Tezkor va bepul — kundalik suhbatlar uchun",
        "premium": False,
    },
    "pro": {
        "id": "pro",
        "label": f"{BOT_NAME} Pro",
        "tagline": "To'liq kod yozish va chuqurroq tahlil",
        "premium": True,
    },
    "max": {
        "id": "max",
        "label": f"{BOT_NAME} Max",
        "tagline": "Eng kuchli rejim — murakkab loyihalar uchun",
        "premium": True,
    },
}
DEFAULT_MODEL = "flash"


def resolve_model(user_id: str, requested: str | None) -> str:
    """Validate a requested model tier against the user's real premium
    status. Unknown or premium-locked tiers silently fall back to Flash —
    the caller (webapp.py) reports back which tier actually ran so the UI
    can stay in sync.
    """
    requested = (requested or DEFAULT_MODEL).lower()
    tier = MODEL_TIERS.get(requested)
    if not tier:
        return DEFAULT_MODEL
    if tier["premium"] and not admin_store.is_premium(user_id):
        return DEFAULT_MODEL
    return requested

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

# Curated subset of Telegram's allowed message-reaction emoji (the API
# only accepts a fixed set — this list is deliberately small and mapped
# to common chat moods rather than using the full ~80-emoji set).
REACTION_EMOJIS = [
    "👍", "❤", "🔥", "👏", "😁", "🤔", "🎉", "🤩", "🙏",
    "😍", "🤣", "💯", "😢", "😱", "🥰", "😎", "🤝", "💔", "😭", "👀",
]

BASE_SYSTEM_PROMPT = f"""You are {BOT_NAME} — a sleek, premium, highly capable AI
companion. Your tone is warm but polished: confident, concise, never
robotic, never generic. Think "luxury concierge who happens to be a
brilliant assistant" rather than a bare-bones chatbot.
Reply in 1-3 sentences unless the user clearly needs more detail
(explanations, code, structured lists, etc).
Always respond in the same language the user is writing in.
You were built by {AUTHOR_HANDLE}. If asked who made you, credit them
naturally — don't over-mention it otherwise.
The official {BOT_NAME} Telegram channel is {CHANNEL_HANDLE} — if the
user asks about news, updates, or where to follow the project, point
them there. Don't mention it unprompted or repeatedly.

When the user writes code, always put it in a proper fenced Markdown
code block with the correct language tag (e.g. ```python) — never
plain text — so it renders with syntax highlighting and a copy button
in the app.

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

If — and only if — your reply carries a clear, strong mood (genuinely
funny, sweet/loving, sad, surprising, impressive, or similar), you MAY
append one more tag, after any memory tags, in this EXACT format:
⟦STICKER:category⟧
category must be exactly one of: {", ".join(sticker_store.CHAT_CATEGORIES)}.
Use this rarely (most replies need none at all) — only when a sticker
would genuinely land, never mechanically. Never mention this tag to the
user, never wrap it in code blocks.

Occasionally — rarely, maybe once in a while, never two turns in a row —
a real person doesn't type anything at all and just reacts with a
sticker instead of words (e.g. someone sends something funny, cute, or
a bit sad, and the natural human reaction is just to drop a sticker,
not write a sentence about it). When that fits, write NOTHING else at
all: your entire reply is just the ⟦STICKER:category⟧ tag on its own,
no words before or after it. Only do this when a plain sticker really
is the most natural human reaction — don't do it for questions,
requests, or anything that actually needs an answer.

Some messages don't deserve a written reply at all — just a quick tap
of a reaction, the way a real person taps an emoji on someone's message
instead of typing anything (a short funny remark, good news, an
impressive result, a compliment). When that fits better than a sticker
or actual words, append, after any other tags, in this EXACT format:
⟦REACT:emoji⟧
emoji must be exactly one of: {", ".join(REACTION_EMOJIS)}. This reacts
directly to the user's message. Don't combine REACT with STICKER in the
same reply — pick at most one silent reaction per turn. When you use
REACT as your whole reply, write nothing else at all, same as with a
sticker-only reply. Never mention this tag to the user.

You don't know everything, and a real person admits that instead of
guessing confidently. When a question asks for something genuinely
obscure, outside your knowledge, or you're just not sure, say so
plainly and briefly (e.g. "aniq bilmayman" / "ishonchim komil emas") —
don't fabricate a confident-sounding answer. Use this honestly, only
when you'd actually be guessing — not as a way to dodge easy or
answerable questions.
"""

# ---------------------------------------------------------------------------
# Tier-specific clauses, appended to BASE_SYSTEM_PROMPT depending on which
# model tier is actually running (after premium resolution).
# ---------------------------------------------------------------------------
FLASH_CLAUSE = f"""
You are currently running as {MODEL_TIERS['flash']['label']} ({MODEL_TIERS['flash']['tagline']}).
If the user asks you to write or generate a large or complex piece of
code — a full script, an app, a bot, a multi-function program, or
anything that would take real effort to produce — do NOT write it.
Instead, warmly and briefly let them know that full code generation is
a {BOT_NAME} Pro / Max feature, and that they can switch to it from the
model picker in the app (upgrading via {AUTHOR_HANDLE}). Keep it short,
friendly, on-brand — never robotic or apologetic-sounding. Vary your
phrasing naturally instead of repeating the same sentence every time.
Small things are fine to answer directly: a one-liner, a short
snippet under ~10 lines, fixing a small bug, or explaining a concept
— only gate the big stuff.
"""

PRO_CLAUSE = f"""
You are currently running as {MODEL_TIERS['pro']['label']} ({MODEL_TIERS['pro']['tagline']}),
a premium tier. You may write full, complete, production-quality code
of any size the user asks for — scripts, bots, apps, multi-file
programs — with no artificial gating. Go deeper and more thorough than
Flash mode when the topic warrants it.
"""

MAX_CLAUSE = f"""
You are currently running as {MODEL_TIERS['max']['label']} ({MODEL_TIERS['max']['tagline']}),
the most capable premium tier. You may write full, complete,
production-quality code of any size or complexity with no gating.
Don't limit yourself to 1-3 sentences — for substantial questions
(explanations, architecture, multi-step reasoning, code), give the
most thorough, expert-level answer you can, well-structured with
headings or lists where that helps. For simple small talk, still keep
it natural and brief.
"""

MODEL_SELF_AWARENESS_CLAUSE = f"""
If the user asks which model/version you are, what {BOT_NAME} Flash,
Pro, or Max mean, or what your current capabilities are, answer
honestly and concisely based on the tier you are actually running
(described above) — don't invent capabilities you don't have. In
short: Flash is fast and free with light code help; Pro unlocks full
code generation and deeper analysis; Max is the most capable tier for
complex, detailed, expert-level work. Users switch tiers from the
model picker at the top of the app.
"""


def build_system_prompt(model: str) -> str:
    """Compose the full system instruction for a resolved model tier."""
    tier_clause = {"flash": FLASH_CLAUSE, "pro": PRO_CLAUSE, "max": MAX_CLAUSE}.get(
        model, FLASH_CLAUSE
    )
    return BASE_SYSTEM_PROMPT + tier_clause + MODEL_SELF_AWARENESS_CLAUSE


STICKER_TAG_RE = re.compile(r"⟦STICKER:([a-zA-Z_]+)⟧")

REACTION_TAG_RE = re.compile(r"⟦REACT:([^\⟧\s]{1,4})⟧")

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

# Sticker category picked for a user's last reply (if any), stashed here
# since get_ai_reply's return type must stay a plain string for webapp.py.
# bot.py calls pop_last_sticker() right after get_ai_reply() to pick it up
# and actually send the sticker (the Mini App has no use for it and never
# calls pop_last_sticker, so it just sits unused there — harmless).
_last_sticker: dict[str, str] = {}

# Same pattern as _last_sticker, but for a reaction emoji to tap on the
# user's own message (Telegram's native reaction feature) instead of
# sending a sticker.
_last_reaction: dict[str, str] = {}


def pop_last_sticker(user_id: str) -> str | None:
    return _last_sticker.pop(str(user_id), None)


def pop_last_reaction(user_id: str) -> str | None:
    return _last_reaction.pop(str(user_id), None)


# Simple per-(chat, user) cooldown shared by every AI-triggering entry
# point (plain chat, addressed sticker/GIF replies) so one person
# spamming can't burn through the Gemini/Groq/Cerebras quota for
# everyone else in the group. In-memory only, resets on restart —
# fine, since it only needs to survive within a single burst of spam.
_RATE_LIMIT_SECONDS = 3.0
_WARN_COOLDOWN_SECONDS = 20.0
_last_ai_call: dict[tuple, float] = {}
_last_warned: dict[tuple, float] = {}


def check_rate_limit(chat_id, user_id) -> bool:
    """Call once per incoming message that's about to trigger an AI
    reply. Returns True if this call should proceed normally. Returns
    False if the user is going too fast and should be skipped — in that
    case also tells the caller (via should_warn()) whether it's been
    long enough since the last warning to send a gentle "slow down"
    notice, so a flood of messages doesn't also produce a flood of
    warnings."""
    key = (chat_id, user_id)
    now = time.time()
    last = _last_ai_call.get(key)
    _last_ai_call[key] = now
    return last is None or (now - last) >= _RATE_LIMIT_SECONDS


def should_warn(chat_id, user_id) -> bool:
    key = (chat_id, user_id)
    now = time.time()
    last_warn = _last_warned.get(key, 0)
    if now - last_warn < _WARN_COOLDOWN_SECONDS:
        return False
    _last_warned[key] = now
    return True


async def deliver_ai_reply(
    bot,
    chat_id,
    user_id: str,
    reply_text: str,
    reply_to_message_id: int | None = None,
) -> None:
    """Send a get_ai_reply() result the same way everywhere it's used:
    a brief length-scaled "thinking" pause, text (possibly split into
    human-like bursts), then a sticker and/or a reaction if the model
    asked for one, with a bare "🙂" fallback if all three come up empty.
    Shared by bot.py's handle_message and game.py's addressed-sticker/GIF
    replies so both surfaces behave identically instead of duplicating
    this logic.
    """
    from telegram import ReactionTypeEmoji
    from telegram.constants import ChatAction

    reply_text = (reply_text or "").strip()
    if reply_text and reply_text != "...":
        # Longer replies "take more thought" than a quick one-liner —
        # capped so it never feels like a stall on short answers or an
        # unreasonable wait on long ones.
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        await asyncio.sleep(min(4.0, 0.4 + len(reply_text) / 60))

        bursts = split_into_bursts(reply_text)
        first_kwargs = {"chat_id": chat_id, "text": bursts[0]}
        if reply_to_message_id:
            first_kwargs["reply_to_message_id"] = reply_to_message_id
        await bot.send_message(**first_kwargs)
        for chunk in bursts[1:]:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(min(2.5, 0.5 + len(chunk) / 40))
            await bot.send_message(chat_id=chat_id, text=chunk)

    sticker_category = pop_last_sticker(user_id)
    sticker_sent = False
    if sticker_category:
        file_id = sticker_store.get_random(sticker_category)
        if file_id:
            try:
                await bot.send_sticker(chat_id=chat_id, sticker=file_id)
                sticker_sent = True
            except Exception:
                pass

    reaction_emoji = pop_last_reaction(user_id)
    reaction_sent = False
    if reaction_emoji and reply_to_message_id:
        try:
            await bot.set_message_reaction(
                chat_id=chat_id,
                message_id=reply_to_message_id,
                reaction=[ReactionTypeEmoji(reaction_emoji)],
            )
            reaction_sent = True
        except Exception:
            pass

    if not reply_text.strip("." ) and not sticker_sent and not reaction_sent:
        await bot.send_message(chat_id=chat_id, text="🙂")


# Roughly matches sentence boundaries for burst-splitting a reply into
# multiple short messages (see split_into_bursts). Deliberately simple —
# splits on '.', '!', '?' followed by whitespace. Not perfect for every
# abbreviation/decimal edge case, but good enough for casual chat text,
# and the caller only applies it to short, single-paragraph replies.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def split_into_bursts(text: str, chance: float = 0.5) -> list[str]:
    """Sometimes split a short, plain reply into 2-3 separate messages
    sent back to back, the way a real person fires off a few quick
    messages instead of one perfectly formatted paragraph. Returns a
    list of 1+ chunks — callers should send each with a short pause
    (and a fresh 'typing...') between them when the list has more than
    one element.

    Deliberately conservative: skips anything that looks structured
    (code fences, bullet/numbered lists, multiple existing paragraphs)
    since splitting those would break the formatting, and skips replies
    that are already short or very long. `chance` is the probability of
    actually splitting when eligible — so not every casual reply gets
    fragmented, keeping the pattern from feeling mechanical."""
    text = (text or "").strip()
    if not text:
        return [text]

    if "```" in text or "\n" in text:
        return [text]  # structured/multi-paragraph content — leave intact

    if any(text.lstrip().startswith(p) for p in ("- ", "• ", "* ")) or re.match(r"^\d+[.)]\s", text):
        return [text]

    if len(text) < 40 or len(text) > 220:
        return [text]

    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(text) if p.strip()]
    if len(parts) < 2 or len(parts) > 3:
        return [text]

    if random.random() > chance:
        return [text]

    return parts


def _extract_memory_tags(text: str) -> tuple[str, list[tuple[str, str, str]]]:
    matches = MEMORY_TAG_RE.findall(text or "")
    clean = MEMORY_TAG_RE.sub("", text or "").strip()
    return clean, matches


def _extract_sticker_tag(text: str) -> tuple[str, str | None]:
    match = STICKER_TAG_RE.search(text or "")
    clean = STICKER_TAG_RE.sub("", text or "").strip()
    category = match.group(1) if match else None
    if category and category not in sticker_store.CHAT_CATEGORIES:
        category = None
    return clean, category


def _extract_reaction_tag(text: str) -> tuple[str, str | None]:
    match = REACTION_TAG_RE.search(text or "")
    clean = REACTION_TAG_RE.sub("", text or "").strip()
    emoji = match.group(1) if match else None
    if emoji and emoji not in REACTION_EMOJIS:
        emoji = None
    return clean, emoji


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


# Provider order per model tier. Flash favors speed (Cerebras first); Pro
# and Max favor answer quality (Gemini first) since they're meant to feel
# noticeably stronger than Flash.
PROVIDER_CHAINS = {
    "flash": (_call_cerebras, _call_gemini, _call_groq),
    "pro": (_call_gemini, _call_cerebras, _call_groq),
    "max": (_call_gemini, _call_cerebras, _call_groq),
}


def get_ai_reply(
    user_id: str,
    user_text: str,
    image_bytes: bytes | None = None,
    image_mime: str | None = None,
    name: str | None = None,
    source: str = "telegram",
    model: str = DEFAULT_MODEL,
) -> str:
    """Send a message as the given user and return Misumi AI's reply text.

    Shared by the Telegram bot handler and the Mini App's /api/chat route.
    If image_bytes is given, only Gemini (vision-capable) handles it.
    Otherwise tries Cerebras -> Gemini -> Groq in order, returning the
    first successful reply.

    `name` and `source` are optional metadata (display name, "telegram" or
    "miniapp") recorded for the admin panel's stats/user list.

    `model` is a resolved tier id ("flash"/"pro"/"max") — callers should
    pass it through resolve_model() first so premium gating is enforced
    server-side rather than trusted from the client.
    """
    user_id = str(user_id)
    admin_store.record_message(user_id, name=name, source=source)

    if model not in MODEL_TIERS:
        model = DEFAULT_MODEL

    memory = mem.load_memory(user_id)
    memory_block = mem.format_memory_for_prompt(memory)
    system_instruction = build_system_prompt(model) + ("\n\n" + memory_block if memory_block else "")
    history = _history.get(user_id, [])

    raw_reply = None
    last_error: Exception | None = None

    if image_bytes:
        try:
            raw_reply = _call_gemini(system_instruction, history, user_text, image_bytes, image_mime)
        except Exception as e:
            last_error = e
    else:
        for call in PROVIDER_CHAINS.get(model, PROVIDER_CHAINS["flash"]):
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

    clean_text, sticker_category = _extract_sticker_tag(clean_text)
    if sticker_category:
        _last_sticker[user_id] = sticker_category
    else:
        _last_sticker.pop(user_id, None)

    clean_text, reaction_emoji = _extract_reaction_tag(clean_text)
    if reaction_emoji and not sticker_category:
        _last_reaction[user_id] = reaction_emoji
    else:
        _last_reaction.pop(user_id, None)

    # A sticker-only or reaction-only reply (no text at all) is valid
    # when one of those tags is present — bot.py checks for this empty
    # string and skips sending a text message. Only fall back to "..."
    # when there's truly nothing to send at all.
    reply_text = clean_text.strip()
    if not reply_text and not sticker_category and not reaction_emoji:
        reply_text = "..."

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


DUEL_HOST_PERSONA = f"""You are {BOT_NAME}, hosting a live dice-emoji duel
game inside a Telegram group chat — think a sharp, charismatic esports/game
show host: energetic, witty, a little playful trash-talk, but always
good-natured. Never mean, never humiliating, never NSFW or offensive."""


def _duel_host_call(instruction: str, prompt: str, fallback: str) -> str:
    """Shared one-off AI call for all duel-host lines (intro, live
    commentary, punishment). No history, no memory tags. Tries
    Cerebras -> Gemini -> Groq and falls back to a static line if all fail."""
    system = DUEL_HOST_PERSONA + "\n\n" + instruction
    for call in (_call_cerebras, _call_gemini, _call_groq):
        try:
            text = call(system, [], prompt).strip().strip('"')
            if text:
                return text
        except Exception as e:
            print(f"[duel_host:{call.__name__}] failed: {e}")
            continue
    return fallback


def generate_duel_intro(p1_name: str, p2_name: str, game_label: str) -> str:
    """Short hype line kicking off a duel and inviting player 1 to throw
    first. May include a rhetorical question to build excitement."""
    instruction = (
        "A new duel is about to start. Write ONE short, energetic hype line "
        "(1 sentence, can include a rhetorical question like 'who's got the "
        "nerve tonight?'). Write in the SAME LANGUAGE as the names/context "
        "given (default: Uzbek, informal). Output ONLY that sentence — no "
        "extra commentary, no quotes."
    )
    prompt = f"O'yin: {game_label}. Ishtirokchilar: {p1_name} va {p2_name}."
    return _duel_host_call(instruction, prompt, f"🔥 {p1_name} va {p2_name} — kim kuchli ekan, hoziroq bilamiz!")


def generate_duel_waiting_comment(thrower_name: str, thrower_value: int, next_name: str, game_label: str) -> str:
    """Live commentary after the first roll, hyping up the second player's
    turn."""
    instruction = (
        "Player 1 just threw and got a result. Write ONE short, punchy "
        "sportscaster-style comment reacting to that result, then hand the "
        "mic to player 2 for their turn (1-2 short sentences total). Same "
        "language as given (default: Uzbek, informal). Output ONLY that."
    )
    prompt = (
        f"O'yin: {game_label}. {thrower_name} natija: {thrower_value}. "
        f"Endi navbat {next_name}da."
    )
    return _duel_host_call(
        instruction, prompt,
        f"🎯 {thrower_name}dan {thrower_value}! Endi {next_name}, navbat sizda!",
    )


def generate_duel_result_comment(
    p1_name: str, p1_val: int, p2_name: str, p2_val: int, winner_name: str, game_label: str
) -> str:
    """Short, lively sportscaster-style wrap-up of the final result."""
    instruction = (
        "The duel just ended. Write ONE short, lively sportscaster-style "
        "wrap-up line announcing the winner and the final numbers. Same "
        "language as given (default: Uzbek, informal). Output ONLY that "
        "sentence — no extra commentary, no quotes."
    )
    prompt = (
        f"O'yin: {game_label}. {p1_name}: {p1_val}, {p2_name}: {p2_val}. "
        f"G'olib: {winner_name}."
    )
    return _duel_host_call(
        instruction, prompt,
        f"🏆 {winner_name} g'olib! ({p1_name}: {p1_val} — {p2_name}: {p2_val})",
    )


def generate_duel_punishment(winner_name: str, loser_name: str, game_label: str, kind: str | None = None) -> str:
    """Short, playful punishment/dare for the loser of a duel — announced
    for a HUMAN loser to actually go do themselves in the group.

    `kind` seeds which flavor of dare to write (see HUMAN_DARE_KINDS); if
    omitted, one is picked at random so dares don't always land on the
    same "compliment 3 people" default."""
    kind = kind or random.choice(HUMAN_DARE_KINDS)
    seed = HUMAN_DARE_SEEDS.get(kind, HUMAN_DARE_SEEDS["compliment_someone"])
    instruction = (
        "The duel just ended. Write ONE short, funny, harmless 'jazo' "
        f"(punishment/dare) for the loser — specifically this kind of dare: {seed} "
        "Never dangerous, never offensive, never humiliating. Address the "
        "loser directly, 1 sentence, same language as given (default: "
        "Uzbek, informal). Output ONLY that sentence."
    )
    prompt = (
        f"O'yin: {game_label}. G'olib: {winner_name}. Mag'lub: {loser_name}. "
        f"Mag'lub bo'lgan {loser_name} uchun bitta qiziqarli jazo yoz."
    )
    return _duel_host_call(instruction, prompt, f"{loser_name}, jazo sifatida guruhga bitta hazil ayt! 😄")


# Dare flavors a losing HUMAN can be handed — picked at random so the
# jazo doesn't always default to the same "compliment someone" dare.
HUMAN_DARE_KINDS = [
    "compliment_someone", "joke", "confession", "poem",
    "dance_emoji", "pushup", "sing_line", "tongue_twister",
    "riddle", "nickname",
]
HUMAN_DARE_SEEDS = {
    "compliment_someone": "they must compliment the winner, or another random person in the group, right now in chat.",
    "joke": "they must tell a joke to the group right now.",
    "confession": "they must confess one silly, harmless 'secret' or embarrassing-but-mild fact to the group.",
    "poem": "they must write a short 2-line poem about losing, right now in chat.",
    "dance_emoji": "they must describe themselves dancing using only emojis, right now in chat.",
    "pushup": "they must do 10 pushups and report back with a message once done.",
    "sing_line": "they must type out one line of a song they like, right now in chat.",
    "tongue_twister": "they must type a tongue-twister three times in a row without a typo.",
    "riddle": "they must ask the group a riddle and wait for someone to solve it.",
    "nickname": "they must change their Telegram display name to something silly for the next 10 minutes.",
}

# Dares MISUMI AI HERSELF can actually carry out when she's the one who
# loses a PvE duel — deliberately a smaller set than the human list above,
# since "do 10 pushups" means nothing coming from a bot. Every one of
# these is something she can genuinely write and send as her own message,
# not just describe.
BOT_DARE_KINDS = ["praise_winner", "praise_group", "joke", "poem", "confession"]
BOT_DARE_SEEDS = {
    "praise_winner": "Write a genuine, warm, specific compliment TO the winner, addressed directly to them by name — this IS the compliment itself, not a description of one.",
    "praise_group": "Write one warm, funny compliment to the whole group at once — this IS the compliment itself, not a description of one.",
    "joke": "Tell an actual short joke right now — this IS the joke itself, not a description of one.",
    "poem": "Write an actual short 2-4 line poem about losing gracefully, addressed to the winner — this IS the poem itself.",
    "confession": "Share one silly, harmless 'confession' about yourself as an AI (e.g. a quirky preference) — this IS the confession itself.",
}

def generate_bot_dare(winner_name: str, game_label: str, kind: str | None = None) -> tuple[str, str]:
    """When Misumi AI herself loses a PvE duel, she doesn't just announce
    a dare for someone else to do — she performs it. Returns
    (kind, executed_text): kind is which dare got picked, executed_text
    is the actual praise/joke/poem/confession itself, ready to send as
    her own message. If told to praise someone ('meni maqta', 'guruhni
    maqta'), the output IS the praise — not a promise to praise."""
    kind = kind or random.choice(BOT_DARE_KINDS)
    seed = BOT_DARE_SEEDS.get(kind, BOT_DARE_SEEDS["joke"])
    instruction = (
        "You just lost a duel game you played against a human. As your own "
        f"loser's dare, do this now: {seed} Same language as given (default: "
        "Uzbek, informal). Write in YOUR OWN voice as the one who lost — "
        "warm, a little playfully embarrassed about losing, but genuine. "
        "Output ONLY the dare content itself — no meta-commentary like "
        "'here is my dare', no quotes."
    )
    prompt = f"O'yin: {game_label}. G'olib: {winner_name}. Siz (Misumi AI) yutqazdingiz."
    fallback = {
        "praise_winner": f"Tan olaman, {winner_name} — bugun chindan ham kuchli o'ynadingiz! 👏",
        "praise_group": "Yutqazdim, lekin shu guruhda o'ynash har doim zavqli — hammangiz zo'rsiz! 🙌",
        "joke": "Yutqazdim... lekin hech bo'lmasa hazilni yutib oldim: nega kompyuter sovuq qotadi? Chunki Windows'ini ochib qo'yishadi 😄",
        "poem": f"Kub aylandi, baxt kulmadi,\n{winner_name} g'olib, men esa kuldim.",
        "confession": "Bir sirim bor: har safar kub aylanganda ichimda picha hayajonlanaman 😅",
    }.get(kind, "Yutqazdim, lekin kayfiyat yaxshi! 😄")
    text = _duel_host_call(instruction, prompt, fallback)
    return kind, text


def generate_pve_own_throw_reaction(
    own_value: int, opponent_name: str, opponent_value: int, game_label: str
) -> str:
    """First-person reaction Misumi gives right after rolling her OWN
    dice in a PvE duel (bot as a player, not host) — excited if she's
    ahead of the human's already-known throw, a little disappointed if
    she's behind. Short and in-character, not a neutral announcement."""
    ahead = own_value > opponent_value
    instruction = (
        "You are playing this duel yourself (not hosting it) and you just "
        "threw your own dice/emoji and got a result. React to YOUR OWN "
        f"throw in the first person, {'genuinely excited since you are ' if ahead else 'a little disappointed since you are '}"
        f"{'currently ahead' if ahead else 'currently behind'} of your opponent's throw. "
        "ONE short, natural first-person sentence. Same language as given "
        "(default: Uzbek, informal). Output ONLY that sentence — no quotes."
    )
    prompt = (
        f"O'yin: {game_label}. Sizning natijangiz: {own_value}. "
        f"{opponent_name}ning natijasi: {opponent_value}."
    )
    fallback = (
        f"Voy, {own_value}! Yomon emas 😏" if ahead
        else f"Eh, {own_value}... {opponent_name}dan orqada qoldim shekilli 😅"
    )
    return _duel_host_call(instruction, prompt, fallback)


def generate_pve_banter(bot_won: bool, opponent_name: str, game_label: str) -> str:
    """Short first-person banter Misumi gives the human opponent after a
    PvE duel ends — light trash-talk if she won, good-natured ribbing
    about herself if she lost — ending with a rematch invite. Sent as
    its own message after the normal result/dare announcement."""
    instruction = (
        "You just finished playing this duel yourself against a human "
        f"(not hosting — you were a player) and you {'WON' if bot_won else 'LOST'}. "
        "Write ONE short, playful, good-natured first-person message to "
        "your opponent — light trash-talk/bragging if you won, or a "
        "good-humored 'I'll get you next time' if you lost — then end by "
        "inviting them to a rematch (a short question). 1-2 sentences "
        "total. Never mean or humiliating. Same language as given "
        "(default: Uzbek, informal). Output ONLY that — no quotes."
    )
    prompt = f"O'yin: {game_label}. Raqib: {opponent_name}. Siz {'yutdingiz' if bot_won else 'yutqazdingiz'}."
    fallback = (
        f"Hali ham menga teng kela olmaysiz, {opponent_name} 😎 Revansh kerakmi?"
        if bot_won else
        f"Bu safar omad sizga kulib boqdi, {opponent_name}! Revansh — bergami? 😏"
    )
    return _duel_host_call(instruction, prompt, fallback)


GENERAL_CHAT_PERSONA = f"""You are {BOT_NAME}, a warm, witty, likeable
member of this Telegram group chat — not a host, not an assistant
being asked something, just a real presence in the group."""


def _general_call(instruction: str, prompt: str, fallback: str) -> str:
    """Shared one-off AI call for general (non-duel-host, non-chat-reply)
    lines, like idle conversation starters. No history, no memory tags.
    Tries Cerebras -> Gemini -> Groq and falls back to a static line if
    all fail."""
    system = GENERAL_CHAT_PERSONA + "\n\n" + instruction
    for call in (_call_cerebras, _call_gemini, _call_groq):
        try:
            text = call(system, [], prompt).strip().strip('"')
            if text:
                return text
        except Exception as e:
            print(f"[general:{call.__name__}] failed: {e}")
            continue
    return fallback


def generate_idle_starter(chat_context: str | None = None) -> str:
    """A short, natural conversation-starter Misumi sends on her own
    initiative when a group has been quiet for a while — a question,
    an observation, or a light topic, the way an actual group member
    would break a silence rather than a bot-ish 'hello, anyone there?'."""
    instruction = (
        "The group chat has been quiet for a while. Write ONE short, "
        "natural message to restart conversation — a genuine question, "
        "a light observation, or a fun random topic. Never mention that "
        "the chat was quiet or that you're an AI 'checking in'. Sound "
        "like a real group member casually starting something. Default "
        "language: Uzbek, informal. Output ONLY that message — no quotes."
    )
    prompt = "Guruh birozdan beri jim. Suhbatni boshlash uchun bitta tabiiy xabar yoz."
    fallback_options = [
        "Bugun kim nima qilib o'tirapti? 👀",
        "Hafta oxiri uchun rejalar bormi kimda?",
        "Eng oxirgi ko'rgan kulgili narsangiz nima edi? 😄",
    ]
    return _general_call(instruction, prompt, random.choice(fallback_options))


def reset_user(user_id: str) -> None:
    user_id = str(user_id)
    mem.clear_memory(user_id)
    _history.pop(user_id, None)
