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
import random
import re

import requests
from google import genai
from google.genai import types

import admin_store
import memory_manager as mem
import sticker_store

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
"""

STICKER_TAG_RE = re.compile(r"⟦STICKER:([a-zA-Z_]+)⟧")

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


def pop_last_sticker(user_id: str) -> str | None:
    return _last_sticker.pop(str(user_id), None)


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

    clean_text, sticker_category = _extract_sticker_tag(clean_text)
    if sticker_category:
        _last_sticker[user_id] = sticker_category
    else:
        _last_sticker.pop(user_id, None)

    # A sticker-only reaction (no text at all) is valid when a sticker
    # tag is present — bot.py checks for this empty string and skips
    # sending a text message, sending only the sticker. Only fall back
    # to "..." when there's truly nothing to send either way.
    reply_text = clean_text.strip()
    if not reply_text and not sticker_category:
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


def reset_user(user_id: str) -> None:
    user_id = str(user_id)
    mem.clear_memory(user_id)
    _history.pop(user_id, None)
