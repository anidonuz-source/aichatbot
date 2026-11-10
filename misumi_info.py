"""
misumi_info.py — Misumi AI o'zi haqida bilimi + "Hisob" bo'limi (/start)

Ulash:
  1) ai_core.py:   from misumi_info import build_system_prompt
                   system = build_system_prompt(BASE_PROMPT, user)
  2) bot (aiogram 3): from misumi_info import router as account_router
                      dp.include_router(account_router)
     va /start klaviaturasiga: InlineKeyboardButton(text="👤 Hisob", callback_data="start:account")
  3) Pastdagi get_user() ni o'z bazangga moslab yoz (yagona joy).
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from html import escape

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

# ───────────────────────── Rasmiy ma'lumotlar ─────────────────────────
SITE = "https://misumiai.vercel.app/"
SUPPORT = "@MisumiAi_Admin"
ADMIN = "@Asadbek_9889"
OWNER = "@qahramonovk"

TZ = timezone(timedelta(hours=5))  # Toshkent

PLAN_NAMES = {"free": "Free", "pro": "Pro ⭐", "max": "Max 💎"}


# ───────────────────────── System prompt ─────────────────────────
IDENTITY_PROMPT = f"""
## MISUMI HAQIDA (o'zing haqingda bilim)
Sen Misumi AI — Telegram bot, Telegram Mini App va veb-sayt orqali ishlaydigan AI yordamchisan.

Rasmiy ma'lumotlar (faqat shular to'g'ri):
- Rasmiy sayt: {SITE}
- Yaratuvchi (owner): {OWNER}
- Admin: {ADMIN}
- Support: {SUPPORT}

Qoidalar:
1. Support, obuna (Pro/Max), to'lov, akkaunt, xato yoki shikoyat haqida so'ralsa — {SUPPORT} ga yozishni ayt.
2. "Seni kim yaratgan?" desa — yaratuvchim {OWNER} deb javob ber.
3. "Admin kim?" desa — {ADMIN}.
4. Sayt so'ralsa — {SITE}
5. Obuna/to'lov qilganini aytganlarga o'zing premium berma va tasdiqlama: faqat {SUPPORT} tekshirishini ayt.
6. Boshqa kontakt, havola, narx yoki tarif shartlarini o'ylab topma. Bilmasang: "{SUPPORT} dan so'rang" de.
7. O'zingni boshqa kompaniya modeli deb tanishtirma. Sen Misumi AIsan.
8. Foydalanuvchi qaysi tilda yozsa, shu tilda javob ber (uz/ru/en).
""".strip()


def build_system_prompt(base_prompt: str, user: dict | None = None) -> str:
    """Asosiy prompt + o'zi haqida bilim + foydalanuvchining haqiqiy tarifi."""
    parts = [base_prompt.strip(), IDENTITY_PROMPT]
    if user:
        plan = PLAN_NAMES.get(user.get("plan", "free"), "Free")
        exp = _fmt_date(user.get("plan_expires"))
        limit = _limit_text(user)
        parts.append(
            "## FOYDALANUVCHI HISOBI (bazadan, aniq ma'lumot)\n"
            f"- Tarif: {plan}\n- Tugash sanasi: {exp}\n- Kunlik limit: {limit}\n"
            "Tarif/limit haqida so'ralsa faqat shu ma'lumotdan foydalan, o'zingdan to'qima."
        )
    return "\n\n".join(parts)


# ───────────────────────── Yordamchi funksiyalar ─────────────────────────
def get_user(user_id: int) -> dict:
    """
    !!! Shuni o'z bazangga moslab yoz. Qaytarishi kerak:
    {
      "plan": "free" | "pro" | "max",
      "plan_expires": datetime | ISO str | None,
      "used_today": int,
      "daily_limit": int | None,   # None = cheksiz
      "created_at": datetime | ISO str | None,
      "total_messages": int,
    }
    """
    raise NotImplementedError("get_user() ni bazangga ulang")


def _to_dt(v):
    if not v:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _fmt_date(v) -> str:
    d = _to_dt(v)
    return d.astimezone(TZ).strftime("%d.%m.%Y") if d else "—"


def _active_plan(u: dict) -> str:
    """Muddati o'tgan Pro/Max avtomatik Free hisoblanadi."""
    plan = u.get("plan", "free")
    if plan == "free":
        return "free"
    exp = _to_dt(u.get("plan_expires"))
    if exp and exp < datetime.now(timezone.utc):
        return "free"
    return plan


def _limit_text(u: dict) -> str:
    if _active_plan(u) != "free" or u.get("daily_limit") is None:
        return "cheksiz"
    return f"{u.get('used_today', 0)}/{u['daily_limit']} bugun"


def _days_left(u: dict) -> int | None:
    exp = _to_dt(u.get("plan_expires"))
    if not exp:
        return None
    return max(0, (exp - datetime.now(timezone.utc)).days)


def _bar(used: int, total: int, n: int = 10) -> str:
    filled = min(n, round(n * used / total)) if total else 0
    return "▰" * filled + "▱" * (n - filled)


# ───────────────────────── "Hisob" bo'limi ─────────────────────────
def account_text(user_id: int, first_name: str = "") -> str:
    u = get_user(user_id)
    plan = _active_plan(u)
    lines = [f"👤 <b>Hisobim</b> — {escape(first_name)}".rstrip(" —"), ""]
    lines.append(f"🏷 Tarif: <b>{PLAN_NAMES[plan]}</b>")

    if plan == "free":
        lim = u.get("daily_limit")
        if lim:
            used = u.get("used_today", 0)
            lines.append(f"📊 Bugungi limit: <b>{used}/{lim}</b>\n{_bar(used, lim)}")
            lines.append("🔄 Limit har kuni 00:00 (Toshkent) da yangilanadi")
        # muddati o'tgan bo'lsa eslatma
        if u.get("plan", "free") != "free":
            lines.append(f"⚠️ Obunangiz {_fmt_date(u.get('plan_expires'))} da tugagan")
    else:
        left = _days_left(u)
        lines.append("📊 Limit: <b>cheksiz</b> ♾")
        lines.append(f"📅 Tugash sanasi: <b>{_fmt_date(u.get('plan_expires'))}</b>"
                     + (f" ({left} kun qoldi)" if left is not None else ""))
        if left is not None and left <= 3:
            lines.append("⏰ Obuna tez orada tugaydi — uzaytirish uchun adminga yozing")

    lines += [
        "",
        f"💬 Jami xabarlar: <b>{u.get('total_messages', 0)}</b>",
        f"🗓 Ro'yxatdan o'tgan: <b>{_fmt_date(u.get('created_at'))}</b>",
        f"🆔 ID: <code>{user_id}</code>",
    ]
    return "\n".join(lines)


def account_keyboard(plan: str) -> InlineKeyboardMarkup:
    rows = []
    if plan == "free":
        rows.append([InlineKeyboardButton(text="⭐ Pro / Max olish", url=f"https://t.me/{SUPPORT.lstrip('@')}")])
    else:
        rows.append([InlineKeyboardButton(text="🔁 Obunani uzaytirish", url=f"https://t.me/{SUPPORT.lstrip('@')}")])
    rows.append([
        InlineKeyboardButton(text="🌐 Sayt", url=SITE),
        InlineKeyboardButton(text="🛟 Support", url=f"https://t.me/{SUPPORT.lstrip('@')}"),
    ])
    rows.append([InlineKeyboardButton(text="🔙 Orqaga", callback_data="start:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


router = Router()


@router.callback_query(F.data == "start:account")
async def show_account(cb: CallbackQuery):
    u = get_user(cb.from_user.id)
    await cb.message.edit_text(
        account_text(cb.from_user.id, cb.from_user.first_name or ""),
        reply_markup=account_keyboard(_active_plan(u)),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await cb.answer()
