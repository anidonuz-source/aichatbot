"""
misumi_info.py — Misumi AI rasmiy ma'lumotlari (yagona manba).

Kutubxonaga bog'liq emas (aiogram ham, telegram ham kerak emas). Ishlatilishi:
  • ai_core.py  — system prompt'ga "o'zing haqingda" bilim va foydalanuvchi tarifi
  • extras.py   — /start va Hisob bo'limida tarif muddati, support/admin/owner/sayt
  • bot.py      — zaxira /start matni
Kontaktlarni o'zgartirish kerak bo'lsa — faqat shu yerda o'zgartiring.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

SITE = "https://misumiai.vercel.app/"
SUPPORT = "@MisumiAi_Admin"   # obuna, to'lov, xato, shikoyat
ADMIN = "@Asadbek_9889"
OWNER = "@qahramonovk"        # yaratuvchi

TZ = timezone(timedelta(hours=5))  # Toshkent


def tg_url(handle: str) -> str:
    return f"https://t.me/{handle.lstrip('@')}"


SUPPORT_URL = tg_url(SUPPORT)

# ───────────────────────── System prompt ─────────────────────────
IDENTITY_PROMPT = f"""
RASMIY MA'LUMOTLAR (faqat shular to'g'ri, so'ralganda ishlat):
- Rasmiy sayt: {SITE}
- Yaratuvchi (owner): {OWNER}
- Admin: {ADMIN}
- Support: {SUPPORT}

Qoidalar:
1. Support, obuna (Pro/Max), to'lov, akkaunt muammosi, xato yoki shikoyat so'ralsa — {SUPPORT} ga yozishni ayt.
2. "Seni kim yaratgan?" desa — yaratuvchim {OWNER} deb javob ber.
3. "Admin kim?" desa — {ADMIN}.
4. Sayt so'ralsa — {SITE}
5. Kimdir to'lov qildim / premium berdim desa — o'zing premium berma va tasdiqlama, {SUPPORT} tekshirishini ayt.
6. Yuqoridagilardan boshqa kontakt, havola, narx yoki tarif shartini o'ylab topma. Bilmasang "{SUPPORT} dan so'rang" de.
7. O'zingni boshqa kompaniya modeli deb tanishtirma.
""".strip()


# ───────────────────────── Tarif holati (admin_store asosida) ─────────────────────────
def plan_status(user_id) -> dict:
    """{'premium': bool, 'until': epoch|None, 'days_left': int|None, 'unlimited': bool}"""
    try:
        import admin_store
        prem = bool(admin_store.is_premium(user_id))
        until = admin_store.premium_until(user_id) if prem else None
    except Exception:
        prem, until = False, None
    days_left = None
    if prem and until:
        days_left = max(0, int((float(until) - time.time()) // 86400))
    return {"premium": prem, "until": until, "days_left": days_left,
            "unlimited": prem and not until}


def fmt_date(epoch) -> str:
    if not epoch:
        return "—"
    return datetime.fromtimestamp(float(epoch), TZ).strftime("%d.%m.%Y")


def account_clause(user_id) -> str:
    """Prompt uchun: foydalanuvchining HAQIQIY tarifi (bazadan)."""
    st = plan_status(user_id)
    if not st["premium"]:
        body = "Tarif: Free (premium faol emas)."
    elif st["unlimited"]:
        body = "Tarif: Premium (Pro/Max), muddatsiz."
    else:
        body = (f"Tarif: Premium (Pro/Max), {fmt_date(st['until'])} gacha "
                f"({st['days_left']} kun qoldi).")
    return ("\n\nFOYDALANUVCHI HISOBI (bazadan, aniq):\n" + body +
            "\nTarif haqida so'ralsa faqat shu ma'lumotdan foydalan, o'zingdan to'qima.")


def identity_clause(user_id=None) -> str:
    out = "\n\n" + IDENTITY_PROMPT
    if user_id:
        out += account_clause(user_id)
    return out + "\n"


# ───────────────────────── Hisob bo'limi uchun matnlar ─────────────────────────
def plan_lines(user_id) -> list[str]:
    """Hisob oynasidagi tarif qatorlari (HTML)."""
    st = plan_status(user_id)
    if not st["premium"]:
        return ["💎 Tarif: <b>Free</b>",
                f"⭐ Pro / Max olish uchun {SUPPORT} ga yozing"]
    lines = ["💎 Tarif: <b>Premium (Pro/Max)</b> ✅"]
    if st["unlimited"]:
        lines.append("📅 Muddat: <b>cheksiz</b> ♾")
    else:
        lines.append(f"📅 Tugash sanasi: <b>{fmt_date(st['until'])}</b> ({st['days_left']} kun qoldi)")
        if st["days_left"] is not None and st["days_left"] <= 3:
            lines.append(f"⏰ Obuna tez orada tugaydi — uzaytirish uchun {SUPPORT} ga yozing")
    return lines


def contacts_block() -> str:
    """Hisob oynasi uchun rasmiy aloqa bloki (HTML)."""
    return (
        "🛟 <b>Aloqa</b>\n"
        f"• Support: {SUPPORT}\n"
        f"• Admin: {ADMIN}\n"
        f"• Yaratuvchi: {OWNER}\n"
        f"• Sayt: {SITE.rstrip('/')}"
    )
