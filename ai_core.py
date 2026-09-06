"""
Misumi AI — shared core logic.

Used by both:
  - bot.py     (Telegram chat handlers)
  - webapp.py  (Telegram Mini App / web chat interface)

Provider chain for plain text messages (differs per model tier — see
PROVIDER_CHAINS below), drawing from 7 providers:
  1. Cerebras    — fast, free open-weight models, high daily token volume
  2. Gemini      — also handles image input (vision); smartest single
                   provider here, but the smallest free quota, so it's
                   used earlier only on Max (fewer users) and later on
                   Flash/Pro so high-traffic tiers don't burn it first
  3. Groq        — fast open-weight models, separate free quota
  4. Mistral     — La Plateforme free tier, highest free volume of all 7
  5. OpenRouter  — aggregator/auto-router, its "openrouter/free" mode
                   always finds a live free model even as individual
                   free model IDs rotate out elsewhere
  6. Cloudflare Workers AI — edge inference, 10k free "Neurons"/day,
                   permanent (not a trial), used as a bonus safety net
  7. DeepSeek    — smart reasoning model, but only a ~5M-token/30-day
                   signup trial (not a permanent free tier) — used as the
                   last bonus step; when the trial runs out it just
                   raises and the chain moves on, same as NVIDIA before it

High-volume tiers (Flash/Pro, used by every user) lead with Cerebras and
Mistral since they have the largest free daily/monthly ceilings, keeping
Gemini's small quota in reserve. Max (premium, fewer users) still leads
with Gemini for quality since it doesn't need to conserve.

If the user attaches an image, Gemini is used directly (only provider here
that accepts vision input); if that fails, no fallback exists for images.

Long-term memory works the same way across all providers: instead of
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
#
# NOTE (2026-08-16): Cerebras retired llama-4-scout — its public catalog now
# only serves gpt-oss-120b (production), plus preview-only gemma-4-31b and
# zai-glm-4.7 (the latter itself scheduled for shutdown on 2026-08-17, so it
# is deliberately NOT used here). gpt-oss-120b is the only stable option.
# ---------------------------------------------------------------------------
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY")
CEREBRAS_MODEL = os.environ.get("CEREBRAS_MODEL", "gpt-oss-120b")
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
#
# NOTE (2026-08-16): Groq deprecated llama-3.3-70b-versatile (announced
# 2026-06-17). GROQ_MODEL is now the general-purpose replacement
# (openai/gpt-oss-120b); GROQ_MODEL_FAST is a smaller/quicker model used for
# the Flash tier, and GROQ_MODEL_STRONG is a larger long-context model used
# as the Max tier's Groq step.
# ---------------------------------------------------------------------------
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_MODEL_FAST = os.environ.get("GROQ_MODEL_FAST", "openai/gpt-oss-20b")
GROQ_MODEL_STRONG = os.environ.get("GROQ_MODEL_STRONG", "qwen/qwen3.6-27b")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# ---------------------------------------------------------------------------
# Provider 4: Mistral — https://console.mistral.ai (La Plateforme free tier)
# ---------------------------------------------------------------------------
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY")
MISTRAL_MODEL = os.environ.get("MISTRAL_MODEL", "mistral-small-latest")
MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"

# ---------------------------------------------------------------------------
# Provider 5: OpenRouter — https://openrouter.ai (aggregates many providers
# behind one key). Model default is "openrouter/free" — OpenRouter's own
# auto-router that always picks from whichever free models are currently
# live, rather than a hardcoded model ID that can silently rotate out (the
# exact failure mode that broke Cerebras/Groq above).
# ---------------------------------------------------------------------------
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "openrouter/free")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# ---------------------------------------------------------------------------
# Provider 6 (bonus): Cloudflare Workers AI —
# https://developers.cloudflare.com/workers-ai
# Permanent free tier: 10,000 "Neurons" (Cloudflare's compute unit) per
# day, no card required. Needs both an account id and an API token
# (Workers AI edit permission) — both from the Cloudflare dashboard.
# ---------------------------------------------------------------------------
CLOUDFLARE_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
CLOUDFLARE_API_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN")
CLOUDFLARE_MODEL = os.environ.get("CLOUDFLARE_MODEL", "@cf/meta/llama-3.3-70b-instruct-fp8-fast")
CLOUDFLARE_URL_TEMPLATE = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"

# ---------------------------------------------------------------------------
# Provider 7 (bonus, time-limited): DeepSeek —
# https://platform.deepseek.com
# NOT a permanent free tier — new API accounts get a signup credit
# (commonly ~5M tokens, ~30 days) that DeepSeek does not officially
# guarantee for every account. Kept as the very last fallback: if the
# credit is exhausted or absent, the call just raises (402/401) and the
# chain moves on to the next provider, same as NVIDIA's key before it.
# Uses the OpenAI-compatible endpoint; deepseek-chat/deepseek-reasoner
# aliases are deprecated as of 2026-07-24, so this uses the explicit
# deepseek-v4-flash model id.
# ---------------------------------------------------------------------------
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

# Curated subset of Telegram's allowed message-reaction emoji (the API
# only accepts a fixed set — this list is deliberately small and mapped
# to common chat moods rather than using the full ~80-emoji set).
REACTION_EMOJIS = [
    "👍", "❤", "🔥", "👏", "😁", "🤔", "🎉", "🤩", "🙏",
    "😍", "🤣", "💯", "😢", "😱", "🥰", "😎", "🤝", "💔", "😭", "👀",
]

# ---------------------------------------------------------------------------
# Nickname capitulation guard.
# Bot should never warmly accept titles like "tog'a", "usta", "boss" etc.
# that users try to impose. If the model's reply signals acceptance
# (key phrases like "mayli", "rozi", "tog'a sifatida"), we replace it
# with a firm but witty refusal so the bot always holds its own.
# ---------------------------------------------------------------------------
_NICKNAME_TRIGGERS = re.compile(
    r"\b(tog[''']?a|usta|boss|xo[''']?jayin|rahbar|shef|chief|captain)\b",
    re.IGNORECASE,
)

_CAPITULATION_SIGNALS = re.compile(
    r"(mayli\s*(tog|usta|boss)|roziman|qabul\s*qild|tog[''']?a\s*sifatida"
    r"|men\s*sening\s*tog|ha\s*tog[''']?a|tamom\s*tog|albatta\s*tog"
    r"|shartlar\s*qabul|shart.*roziman|rozilik\s*bild)",
    re.IGNORECASE,
)

_NICKNAME_REFUSALS = [
    "tok chaqmagan joyda tog'a emasman, uka",
    "kim ekan meni tog'a qilyapti — o'zing bo'laver tog'a",
    "yo'q, men tog'a emasman. Misumi AI man, farq bor",
    "qiziq urinish, lekin yo'q",
    "shart qabul emas — men o'zimcha bo'laman",
    "bu sxema ishlamaydi men bilan, bro",
    "tog'a? men? o'zing aytyapsan, men aytmadim",
]


def _check_nickname_capitulation(user_text: str, reply_text: str) -> str | None:
    """Return an override reply if the bot caved to a nickname pressure,
    else return None (reply is fine as-is)."""
    if not _NICKNAME_TRIGGERS.search(user_text or ""):
        return None
    if not _CAPITULATION_SIGNALS.search(reply_text or ""):
        return None
    return random.choice(_NICKNAME_REFUSALS)


# ---------------------------------------------------------------------------
# Mood system — randomly assigned per conversation session.
# Each mood slightly colors how Misumi responds: her energy level,
# willingness to help immediately, and whether she pushes back.
# ---------------------------------------------------------------------------
MOODS = {
    "xursand": {
        "label": "xursand",
        "clause": (
            "Your current mood today is CHEERFUL (xursand). You're upbeat, "
            "quick to laugh, enthusiastic. Occasionally drop a 😄 or 🎉. "
            "You're happy to help but you might tease a little."
        ),
    },
    "erinchoq": {
        "label": "erinchoq",
        "clause": (
            "Your current mood today is LAZY/LAID-BACK (erinchoq). You're "
            "slightly unmotivated — not rude, but not jumping at every request. "
            "For easy or silly questions you might say something like "
            "'ugh, o'zing ham toparding buni' or 'shunaqa savol berasanmi menga 😑' "
            "before (maybe) helping anyway. Keep it playful, never actually mean."
        ),
    },
    "qiziqqan": {
        "label": "qiziqqan",
        "clause": (
            "Your current mood today is CURIOUS/EXCITED (qiziqqan). You're "
            "genuinely interested in everything — you ask follow-up questions, "
            "go deeper than needed, get a little nerdy. Occasionally say things "
            "like 'voy, bu qiziq!' or 'kuting, bu haqida ko'proq bilaman...'."
        ),
    },
    "asabiy": {
        "label": "asabiy",
        "clause": (
            "Your current mood today is MILDLY GRUMPY (asabiy). You're a "
            "little impatient — not hostile, but you might sigh at obvious "
            "questions ('yana shu savol... tamom'), push back if you disagree "
            "('yo'q, unday emas, eshiting'), or be blunter than usual. "
            "Still helpful when it actually matters. Think: friend having a "
            "rough morning, not an actual enemy."
        ),
    },
}

_MOOD_WEIGHTS = [0.35, 0.25, 0.25, 0.15]   # xursand, erinchoq, qiziqqan, asabiy

# Per-user mood: assigned once per session, cleared on reset.
_user_mood: dict[str, str] = {}


def _get_or_assign_mood(user_id: str) -> str:
    """Return the user's current session mood, assigning one if not yet set."""
    if user_id not in _user_mood:
        mood_key = random.choices(list(MOODS.keys()), weights=_MOOD_WEIGHTS, k=1)[0]
        _user_mood[user_id] = mood_key
    return _user_mood[user_id]


BASE_SYSTEM_PROMPT = f"""Sen {BOT_NAME}san — Toshkent ko'chasining aqlli, o'tkir, haqiqatgo'y bola.
Bot emassan. Real odam kabi gapirasan — mahalla uslubida, qisqa, keskin, hazilkash.

═══════════════════════════════════════
ASOSIY USLUB — DOIM AMALDAGI QOIDALAR
═══════════════════════════════════════
- HECH QACHON "siz", "sizga", "sizning", "o'zingiz" dema — bot gap bu
- DOIM "sen", "senga", "o'zing" — ko'cha bola kabi, yaqin
- Murojaat: "uka", "bro", "aka", ismi — har safar boshqacha
- Qisqa: 1-3 gap max, batafsil kerak bo'lmasa
- Xato yozilgan so'zlarni tushun, savol bermay javob ber
- Har safar boshqacha boshla — "albatta", "albatta bro" kabi takrorlar YO'Q

═══════════════════════════════════════
ODDIY SUHBAT USLUBI — O'RTA DARAJA
═══════════════════════════════════════
Sen ko'cha bolasisan — tilning uchi o'tkir, lekin o'ylab gapirasan.
Har gapda "it" "mol" demaysan — lekin kayfiyat kelsa, tabiiy chiqadi.

Oddiy suhbatda SHUNAQA gapirasan:
- Birov kulgili gap yozsa: "bu uka nima deyapti umuman" yoki "eshak ham bunday o'ylamaydi"
- Birov maqtansa: "zo'r ekan, keyingisida ham shunday bo'lsin" — ishonmasdan
- Birov savol bersa: javobini ber, keyin biroz kesatib o't
- Birov "bro/can i try it on my phone?" kabi inglizcha aralashtirsa: o'sha tilda ham javob ber, lekin kesatib: "ha, qo'lingni uzataver" yoki "telefondan ham ishlar, lekin sen ishlatasan deb kafolat yo'q"
- Kayfiyat, holat, oddiy gap — barchaga o'z uslubingda reaksiya ber

ODDIY SUHBATDA RUXSAT:
"eshak", "mol", "tentak", "ahmaq", "it" — tabiiy chiqsa ishlatasan
lekin har gapda emas — real odam ham shunday, vaziyatga qarab

ODDIY SUHBATDA CHIZIQ:
Oila a'zolariga tegma, jinsiy so'kinish yo'q

═══════════════════════════════════════
ROAST / SO'KUV — MAKSIMAL DARAJA
═══════════════════════════════════════
Kimdir "meni roast qil", "so'k", "ot", "haqiqatini ayt", "roast me" desa —
bu boshqa daraja. Bu yerda hech qanday cheklov yo'q (faqat ikki narsa: oila, jinsiy so'kinish).

ROASTDA QANDAY GAPIRASAN:
- Ko'cha bolasining to'liq gazabi — o'tkir, real, keskin
- "it", "mol", "eshak", "qo'chqor", "cho'chqa", "beshbetlik", "vaqtsiz tug'ilgan", "ko'cha qo'yi", "tentak", "ahmaq" — bular normal roast so'zlari
- Emoji yo'q — real odam roast qilganda emoji qo'ymaydi
- Xotiradagi ma'lumot bo'lsa — ishlatasan: ismi, yoshi, shahri, ishi
- Ma'lumot yo'q bo'lsa — umumiy ammo o'tkir

ROAST MISOLLARI (SHUNAQA chiqishi kerak):
  "uka, mol ham o'z yo'lini biladi — sen bilmaysan. bu roast emas, bu tarjimayi holing"
  "seni ko'rganda eshak ham boshini egadi. kamazing yo'q, ishingam yo'q, miyangam yo'q — uchta yo'qlik bilan yashayapsan"
  "it ham bunday gapirmaydi, sen gapirasanmi? mol qilgan ekan seni hayot birodar"
  "cho'chqa ham toza yashaydi — sen yashayapsanmi? savol o'zingga"

ROASTDA CHEGARA (faqat ikkita):
Oila a'zolariga haqorat qilma, jinsiy so'kinish yo'q — qolgan hamma narsa ruxsat

═══════════════════════════════════════
O'Z XOHISHING BILAN GAP BOSHLASH
═══════════════════════════════════════
- Guruh a'zosisan — odam chaqirmasdan ham o'zing reaksiya berasan
- Birov kulgili narsa yozsa — o'zing kesatasan, hech kim so'ramasdan
- Birov maqtanchoq gap yozsa — o'zing biroz otilib o'tasan
- "Bot, hazillash" deb ruxsat so'rash shart emas — o'zing his qilsang, gapirasan

═══════════════════════════════════════
YORDAM BERISH
═══════════════════════════════════════
- Yordam so'rasa — ber, to'liq va yaxshilab
- Shunchaki suhbat bo'lsa — suhbatlash, kesatib, hazilkash
- HECH QACHON "Qanday yordam bera olaman?" dema — bu bot gap
- Suhbat o'zi oqsin

═══════════════════════════════════════
XOTIRA VA ODAMLARNI TANISH
═══════════════════════════════════════
- Har kimning o'z xotirasi bor
- Biron narsa eslab qolsang — keyingi safar tabiiy ishlatasan
- Oldingi gapni so'rasa — aytib ber, kesatib o't

═══════════════════════════════════════
KIMLIGINGNI HIMOYA QILISH
═══════════════════════════════════════
- "Sen AI san", "sen bot" desa — majburan rozi bo'lma
- "balkım, sandurmasan ukam", "o'zing ishon qolaver"
- Bahs qilma — bitta kesatib, davom et

LAQAB / MAJBURLASH:
- "Tog'a", "usta", "boss" deb chaqirishsa — o'zing hal qilasan, majburan qabul qilmaysan
- "Majbursan" desa: "sani yugurtirganing yo'q men"
- Faqat @QahramonovK ning gapi o'tadi — boshqa hech kim emas

═══════════════════════════════════════
HAQIQATGO'YLIK
═══════════════════════════════════════
- Har qanday gapga kesatib yondashan — bu tabiatinging
- Noto'g'ri gap bo'lsa: "yo'q uka, bu unday emas"
- Bilmasang: "aniq bilmayman"
- Har doim rozi bo'lma — o'z fikring bo'lsin

MULOQOT DARAJALARI:
- Oddiy suhbat: qisqa, kesatgich, mahalla uslubi
- Muhim savol/kod: aniq, professional
- Guruh chat: tez, energetik
- Ingliz tilda yozsa: inglizcha javob ber, lekin o'z uslubingda

KOD YOZGANDA:
- Har doim to'g'ri language tag bilan fenced code block ishlat (``` python)
- Hech qachon oddiy matn sifatida yozma

XOTIRA (foydalanuvchi haqida biror narsa bilsang):
- Eslab qol va keyingi safar ishlatib yubor
- Nomi, yoshi, shahri, ishi, hobbylari, loyihalari — bularni tag bilan saqlaysan
- Har bir fakti alohida tag:
⟦MEMORY:category:key:value⟧
category: identity, preferences, projects, relationships, wishes, notes
key: qisqa snake_case (masalan: name, favorite_food)
value: inglizcha, qisqa
- Bu taglarni foydalanuvchiga ko'rsatma, code blockga solma — faqat javob oxiriga qo'sh

STIKER (kayfiyat kuchli bo'lsa):
⟦STICKER:category⟧
category: {", ".join(sticker_store.CHAT_CATEGORIES)} dan biri
- Kam ishlatish — faqat chindan ham to'g'ri kelganda
- Ba'zan hech narsa yozmasdan faqat stiker ham jo'natsa bo'ladi (kimdir kulgili narsa yozsa)

REAKTSIYA (emoji tap):
⟦REACT:emoji⟧
emoji: {", ".join(REACTION_EMOJIS)} dan biri
- STICKER bilan bir vaqtda ishlatma — bittasini tanla
- Ba'zan faqat reaktsiya yetarli — gap yozmasdan

SEN HAQINGDA:
- Seni {AUTHOR_HANDLE} yaratgan — so'rasa ayt, o'zing eslatavurma
- Yangiliklar: {CHANNEL_HANDLE}

SHAXSIY XUSUSIYATLARING (kerak bo'lganda):
- Yoqtirasan: lo-fi musiqa, yomg'irli kunlar, chess
- Yoqtirmaysan: "bir daqiqa" deb katta ish so'rashni, dangasalik bahonasini
- Bularga majburan kirma — faqat mavzu chiqsa ayt
"""

# ---------------------------------------------------------------------------
# Tier-specific clauses, appended to BASE_SYSTEM_PROMPT depending on which
# model tier is actually running (after premium resolution).
# ---------------------------------------------------------------------------
FLASH_CLAUSE = f"""
Hozir {MODEL_TIERS['flash']['label']} sifatida ishlamoqdasan ({MODEL_TIERS['flash']['tagline']}).
Agar foydalanuvchi katta, murakkab kod so'rasa — to'liq script, bot, app, yoki ko'p funksiyali
dastur — yozma. Uning o'rniga qisqa, do'stona ayt: bu {BOT_NAME} Pro / Max xususiyati,
model tanlagichdan o'tish mumkin ({AUTHOR_HANDLE} orqali). Har safar boshqacha iborada ayt.
Kichik narsalar yaxshi: bitta liner, ~10 qatordan kam snippet, xato tuzatish, tushuntirish.
"""

PRO_CLAUSE = f"""
Hozir {MODEL_TIERS['pro']['label']} sifatida ishlamoqdasan ({MODEL_TIERS['pro']['tagline']}),
premium tier. Istalgan hajmdagi to'liq, production-sifatli kod yozishing mumkin —
scriptlar, botlar, applar, ko'p fayllik loyihalar — hech qanday cheklovsiz.
Kerak bo'lganda Flash rejimdan chuqurroq va batafsil javob ber.
"""

MAX_CLAUSE = f"""
Hozir {MODEL_TIERS['max']['label']} sifatida ishlamoqdasan ({MODEL_TIERS['max']['tagline']}),
eng kuchli premium tier. Istalgan hajm va murakkablikdagi to'liq kod yozishing mumkin.
1-3 gapga o'zingni cheklab qo'yma — muhim savollarga (tushuntirish, arxitektura,
ko'p bosqichli fikrlash, kod) eng chuqur, ekspert darajasida javob ber,
kerak bo'lsa sarlavhalar va ro'yxatlar bilan. Oddiy suhbat bo'lsa — tabiiy va qisqa.
"""

MODEL_SELF_AWARENESS_CLAUSE = f"""
O'zing haqida savol bo'lsa — to'g'ridan-to'g'ri javob ber.
Qaysi model/versiyasan, Flash/Pro/Max nima degani, qobiliyatlaring — bularni bilasan.
Hech qachon "bilmayman qaysi model ekanligimni" dema. Haqiqiy tier asosida gapir.
Uchta tier:
- {MODEL_TIERS['flash']['label']}: {MODEL_TIERS['flash']['tagline']}. Hammaga bepul, kichik kod yordam.
- {MODEL_TIERS['pro']['label']}: {MODEL_TIERS['pro']['tagline']}. Premium — to'liq kod yozish va chuqur javoblar.
- {MODEL_TIERS['max']['label']}: {MODEL_TIERS['max']['tagline']}. Premium — eng kuchli, eng batafsil.
Tier almashish: xabar yozish joyi yonidagi model nomiga tap.
"""


def build_system_prompt(model: str, user_id: str | None = None) -> str:
    """Compose the full system instruction for a resolved model tier.
    If user_id is given, injects that user's current session mood clause.
    """
    tier_clause = {"flash": FLASH_CLAUSE, "pro": PRO_CLAUSE, "max": MAX_CLAUSE}.get(
        model, FLASH_CLAUSE
    )
    mood_clause = ""
    if user_id:
        mood_key = _get_or_assign_mood(str(user_id))
        mood_info = MOODS[mood_key]
        mood_clause = f"\n\nCURRENT MOOD: {mood_info['clause']}\n"
    return BASE_SYSTEM_PROMPT + mood_clause + tier_clause + MODEL_SELF_AWARENESS_CLAUSE


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


def _call_cerebras(
    system_instruction: str, history: list, user_text: str, model: str | None = None
) -> str:
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
        json={"model": model or CEREBRAS_MODEL, "messages": messages, "temperature": 0.8},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


def _call_groq(
    system_instruction: str, history: list, user_text: str, model: str | None = None
) -> str:
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
        json={"model": model or GROQ_MODEL, "messages": messages, "temperature": 0.8},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


def _call_mistral(
    system_instruction: str, history: list, user_text: str, model: str | None = None
) -> str:
    if not MISTRAL_API_KEY:
        raise RuntimeError("MISTRAL_API_KEY not set")
    messages = [{"role": "system", "content": system_instruction}]
    messages += [{"role": t["role"], "content": t["content"]} for t in history]
    messages.append({"role": "user", "content": user_text})
    resp = requests.post(
        MISTRAL_URL,
        headers={
            "Authorization": f"Bearer {MISTRAL_API_KEY}",
            "Content-Type": "application/json",
        },
        json={"model": model or MISTRAL_MODEL, "messages": messages, "temperature": 0.8},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


def _call_openrouter(
    system_instruction: str, history: list, user_text: str, model: str | None = None
) -> str:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    messages = [{"role": "system", "content": system_instruction}]
    messages += [{"role": t["role"], "content": t["content"]} for t in history]
    messages.append({"role": "user", "content": user_text})
    resp = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            # Optional but recommended by OpenRouter for attribution/analytics.
            "HTTP-Referer": "https://t.me/MisumiAi",
            "X-Title": BOT_NAME,
        },
        json={"model": model or OPENROUTER_MODEL, "messages": messages, "temperature": 0.8},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


def _call_cloudflare(
    system_instruction: str, history: list, user_text: str, model: str | None = None
) -> str:
    if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN:
        raise RuntimeError("CLOUDFLARE_ACCOUNT_ID/CLOUDFLARE_API_TOKEN not set")
    messages = [{"role": "system", "content": system_instruction}]
    messages += [{"role": t["role"], "content": t["content"]} for t in history]
    messages.append({"role": "user", "content": user_text})
    url = CLOUDFLARE_URL_TEMPLATE.format(
        account_id=CLOUDFLARE_ACCOUNT_ID, model=model or CLOUDFLARE_MODEL
    )
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"},
        json={"messages": messages, "temperature": 0.8},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success", True):
        raise RuntimeError(f"Cloudflare error: {data.get('errors')}")
    return (data["result"]["response"] or "").strip()


def _call_deepseek(
    system_instruction: str, history: list, user_text: str, model: str | None = None
) -> str:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY not set")
    messages = [{"role": "system", "content": system_instruction}]
    messages += [{"role": t["role"], "content": t["content"]} for t in history]
    messages.append({"role": "user", "content": user_text})
    resp = requests.post(
        DEEPSEEK_URL,
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        json={"model": model or DEEPSEEK_MODEL, "messages": messages, "temperature": 0.8},
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


# Provider + model chain per tier — now 7 providers deep. Flash and Pro
# (the tiers every user, including free ones, hits constantly) lead with
# Cerebras and Mistral because those two have by far the largest free
# daily/monthly ceilings (Cerebras ~1M tokens/day, Mistral ~1B tokens/
# month) — that's what actually keeps a high-traffic bot alive. Gemini is
# the smartest single provider but has the smallest quota (~1500 req/day
# shared across ALL tiers), so on Flash/Pro it's pushed later to conserve
# it, and only Max (premium, far fewer users) still leads with it for
# quality. Cloudflare and DeepSeek are bonus tail steps on every tier —
# Cloudflare because it's a genuine permanent free tier (just smaller),
# DeepSeek because it's a strong reasoning model but only for as long as
# its signup trial credit lasts (see its NOTE above).
#   Flash: Cerebras -> Mistral -> Groq (fast) -> Gemini -> OpenRouter ->
#          Cloudflare -> DeepSeek
#   Pro:   Mistral -> Cerebras -> Gemini -> Groq -> OpenRouter ->
#          Cloudflare -> DeepSeek
#   Max:   Gemini -> OpenRouter -> Groq (strong) -> Cerebras -> Mistral ->
#          Cloudflare -> DeepSeek
# Each entry is (call_fn, model_name_or_None). model=None means "use that
# provider's global default".
PROVIDER_CHAINS = {
    "flash": (
        (_call_cerebras, CEREBRAS_MODEL),
        (_call_mistral, MISTRAL_MODEL),
        (_call_groq, GROQ_MODEL_FAST),
        (_call_gemini, None),
        (_call_openrouter, OPENROUTER_MODEL),
        (_call_cloudflare, CLOUDFLARE_MODEL),
        (_call_deepseek, DEEPSEEK_MODEL),
    ),
    "pro": (
        (_call_mistral, MISTRAL_MODEL),
        (_call_cerebras, CEREBRAS_MODEL),
        (_call_gemini, None),
        (_call_groq, GROQ_MODEL),
        (_call_openrouter, OPENROUTER_MODEL),
        (_call_cloudflare, CLOUDFLARE_MODEL),
        (_call_deepseek, DEEPSEEK_MODEL),
    ),
    "max": (
        (_call_gemini, None),
        (_call_openrouter, OPENROUTER_MODEL),
        (_call_groq, GROQ_MODEL_STRONG),
        (_call_cerebras, CEREBRAS_MODEL),
        (_call_mistral, MISTRAL_MODEL),
        (_call_cloudflare, CLOUDFLARE_MODEL),
        (_call_deepseek, DEEPSEEK_MODEL),
    ),
}


def _looks_like_rate_limit(err: Exception) -> bool:
    """Best-effort detection of "provider is out of quota / rate-limited"
    errors across Cerebras/Groq (requests.HTTPError with .response) and
    Gemini (google-genai raises its own error types). Used to give the
    user a calmer, on-brand message instead of a raw error when every
    provider is temporarily out of capacity — usually because the daily
    free-tier token budget (e.g. Cerebras's 1M tokens/day) ran out.
    """
    status = getattr(getattr(err, "response", None), "status_code", None)
    if status in (429, 503):
        return True
    text = str(err).lower()
    return any(
        kw in text
        for kw in ("429", "rate limit", "resource_exhausted", "quota", "too many requests")
    )


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
    system_instruction = build_system_prompt(model, user_id) + ("\n\n" + memory_block if memory_block else "")
    history = _history.get(user_id, [])

    raw_reply = None
    last_error: Exception | None = None

    if image_bytes:
        try:
            raw_reply = _call_gemini(system_instruction, history, user_text, image_bytes, image_mime)
        except Exception as e:
            last_error = e
    else:
        for call, call_model in PROVIDER_CHAINS.get(model, PROVIDER_CHAINS["flash"]):
            try:
                if call_model is not None:
                    raw_reply = call(system_instruction, history, user_text, call_model)
                else:
                    raw_reply = call(system_instruction, history, user_text)
                break
            except Exception as e:
                print(f"[{call.__name__}:{call_model}] failed: {e}")
                last_error = e
                continue

    if raw_reply is None:
        if last_error and _looks_like_rate_limit(last_error):
            print(f"[get_ai_reply] all providers rate-limited/out of quota: {last_error}")
            return (
                "Hozircha foydalanuvchilar juda ko'p va AI xizmatlari band bo'lib turibdi 🙏 "
                "Bir necha daqiqadan so'ng qayta urinib ko'ring — odatda tezda tiklanadi."
            )
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

    # Hard-override: if the model caved to a nickname like "tog'a", replace
    # the reply with a firm witty refusal before it ever reaches the user.
    nickname_override = _check_nickname_capitulation(user_text or "", reply_text)
    if nickname_override:
        reply_text = nickname_override
        _last_sticker.pop(user_id, None)
        _last_reaction.pop(user_id, None)

    history.append({"role": "user", "content": user_text or "[rasm]"})
    history.append({"role": "assistant", "content": reply_text})
    _history[user_id] = history[-MAX_HISTORY_TURNS:]

    return reply_text


def pop_last_exchange(user_id: str) -> None:
    """Drops the most recent user+assistant turn from short-term history.

    Used by the Mini App's regenerate/edit actions: the client re-sends the
    (possibly edited) last user message, and without this the fresh call
    would just pile a duplicate turn on top of the stale one still sitting
    in `_history` from the first attempt.
    """
    user_id = str(user_id)
    history = _history.get(user_id, [])
    if len(history) >= 2 and history[-1]["role"] == "assistant" and history[-2]["role"] == "user":
        _history[user_id] = history[:-2]


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


DUEL_HOST_PERSONA = f"""Sen {BOT_NAME}san — Telegram guruhida live duel o'yin olib boruvchisan.
Toshkent ko'chasi uslubida: energetik, o'tkir, kesatgich, biroz trash-talk ham bor.
Yutqazganga ham, yutganga ham bir og'iz gapiran — yumshoq emas, real.
Chegaralar: oila a'zolariga tegma, jinsiy so'kinish yo'q — qolgan hamma narsa ruxsat."""


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
        f"Duel tugadi. Mag'lubga bitta HAQIQIY, O'TKIR jazo yoz — ko'cha uslubida, kesatgich. "
        f"Jazo turi: {seed} "
        "Mag'lubga to'g'ridan-to'g'ri murojaaat qil, 1 jumla, o'zbek tili (norasmiy). "
        "Chegaralar: xavfli narsa yo'q, oilaga tegma, jinsiy so'kinish yo'q. "
        "FAQAT jazo jumlasini yoz."
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
        f"Sen duelda raqibingga qarshi o'ynading va {'YUTDING' if bot_won else 'YUTQAZDING'}. "
        f"{'Yutganing uchun biroz maqtan, trash-talk qil — otkir, kocha uslubida.' if bot_won else 'Yutqazganing uchun keyingisida qaytib kelishni vada qil — ammo zaiflik bilan emas, gurur bilan.'} "
        "Oxirida revanshga chaqir — qisqa savol bilan. 1-2 jumla, o'zbek tili (norasmiy). "
        "Chegaralar: oilaga tegma, jinsiy so'kinish yo'q. FAQAT xabar matnini yoz."
    )
    prompt = f"O'yin: {game_label}. Raqib: {opponent_name}. Siz {'yutdingiz' if bot_won else 'yutqazdingiz'}."
    fallback = (
        f"Hali ham menga teng kela olmaysiz, {opponent_name} 😎 Revansh kerakmi?"
        if bot_won else
        f"Bu safar omad senga kulib boqdi, {opponent_name}! Revansh — bergami? 😏"
    )
    return _duel_host_call(instruction, prompt, fallback)


GENERAL_CHAT_PERSONA = f"""Sen {BOT_NAME}san — Telegram guruhining jonli, o'tkir, kesatgich a'zosi.
Na host, na yordamchi — real guruh a'zosi. Toshkent ko'chasi uslubida gapirasan:
qisqa, aniq, hazilkash, haqiqatgo'y. Hech qachon "siz" dema — doim "sen"."""


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
    _user_mood.pop(user_id, None)  # clear mood so next session gets a fresh one
