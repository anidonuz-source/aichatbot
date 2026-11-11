"""
Misumi AI — Telegram Stars (XTR) orqali avtomatik to'lov.

Oqim:
  "💎 Pro sotib olish" (ub:buy_pro / ub:buy_max)
    -> tarif tanlash menyusi (⭐ Stars / qo'lda to'lov)
    -> send_invoice (XTR)  -> PreCheckoutQuery -> SUCCESSFUL_PAYMENT
    -> admin_store.grant_premium(user, kun)  -> foydalanuvchi va adminga xabar

Stars uchun provider_token KERAK EMAS (bo'sh qoldiriladi), BotFather'da
hech narsa yoqish shart emas.

Env (ixtiyoriy):
  STARS_PRO_30    Pro, 30 kun narxi (Stars)   default 50
  STARS_PRO_90    Pro, 90 kun narxi (Stars)   default 130
  STARS_MAX_30    Max, 30 kun narxi (Stars)   default 110
  ADMIN_ID        admin chat_id (to'lov xabarnomasi va /refund uchun)

Admin buyruqlari:
  /payments            — oxirgi to'lovlar
  /refund <user_id> <charge_id>   — Stars'ni qaytarish va Pro'ni olib tashlash
Foydalanuvchi: /paysupport  (Telegram talabi bo'yicha majburiy)
"""
import asyncio
import logging
import os
import time

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Update,
)
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

import admin_store
import db
import misumi_info

logger = logging.getLogger("misumi-payments")

ADMIN_ID = os.environ.get("ADMIN_ID", "").strip()
STORE = "payments"


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


# plan_id -> (sarlavha, kun, Stars narxi, UZS narxi — faqat ko'rsatish uchun)
PLANS = {
    "pro30": ("Misumi AI Pro — 30 kun", 30, _int_env("STARS_PRO_30", 50), "30 000"),
    "pro90": ("Misumi AI Pro — 90 kun", 90, _int_env("STARS_PRO_90", 130), "80 000"),
    "max30": ("Misumi AI Max — 30 kun", 30, _int_env("STARS_MAX_30", 110), "70 000"),
}


def _is_admin(user_id) -> bool:
    return bool(ADMIN_ID) and str(user_id) == ADMIN_ID


# ───────────────────────── tarif menyusi ─────────────────────────

def _plans_kb(highlight: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    for pid, (title, days, stars, _uzs) in PLANS.items():
        mark = "👉 " if pid.startswith(highlight or "~") else ""
        rows.append([InlineKeyboardButton(f"{mark}⭐ {title} — {stars} Stars", callback_data=f"pay:stars:{pid}")])
    rows.append([InlineKeyboardButton("💳 Click/Payme (qo'lda, support orqali)", callback_data="pay:manual")])
    rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="ub:services")])
    return InlineKeyboardMarkup(rows)


async def buy_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ub:buy_pro / ub:buy_max — eski 'adminga so'rov' o'rniga tarif menyusi."""
    query = update.callback_query
    await query.answer()
    hl = "max" if query.data.endswith("max") else "pro"
    until = admin_store.premium_until(update.effective_user.id)
    extra = ""
    if admin_store.is_premium(update.effective_user.id):
        extra = "\n\n✅ Sizda Pro allaqachon faol — yangi to'lov muddatni <b>ustiga qo'shiladi</b>."
    text = (
        "💎 <b>Misumi AI Pro</b>\n\n"
        "Telegram Stars bilan to'lang — Pro <b>bir zumda</b> avtomatik yoqiladi.\n"
        "Stars'ni Telegram'ning o'zida (Sozlamalar → Stars) karta orqali sotib olish mumkin."
        f"{extra}"
    )
    try:
        await query.edit_message_text(text, reply_markup=_plans_kb(hl), parse_mode="HTML")
    except Exception:
        await context.bot.send_message(
            update.effective_chat.id, text, reply_markup=_plans_kb(hl), parse_mode="HTML"
        )
    raise ApplicationHandlerStop  # eski ub_callback_router ga o'tmasin


async def stars_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    pid = query.data.split(":", 2)[2]
    plan = PLANS.get(pid)
    if not plan:
        await query.answer("Tarif topilmadi", show_alert=True)
        return
    title, days, stars, _ = plan
    user = update.effective_user
    await query.answer()
    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title=title,
        description=f"{days} kun Misumi AI Pro: barcha Pro xizmatlar, kuchliroq modellar. To'lovdan keyin avtomatik yoqiladi.",
        payload=f"{pid}:{user.id}",
        provider_token="",  # Stars uchun bo'sh
        currency="XTR",
        prices=[LabeledPrice(title, stars)],
    )


async def manual_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Eski qo'lda oqim: adminga tasdiqlash tugmasi bilan so'rov ketadi."""
    query = update.callback_query
    user = update.effective_user
    await query.answer(
        f"So'rov adminga yuborildi ✅ Tezroq bo'lishi uchun {misumi_info.SUPPORT} ga ham yozing",
        show_alert=True,
    )
    if ADMIN_ID:
        uname = f"@{user.username}" if user.username else user.full_name
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ Tasdiqlash (Pro 30 kun)", callback_data=f"ub:approve:{user.id}")]]
        )
        try:
            await context.bot.send_message(
                int(ADMIN_ID),
                f"💎 Qo'lda Pro so'rovi\n\nFoydalanuvchi: {uname} (ID: {user.id})",
                reply_markup=kb,
            )
        except Exception:
            logger.exception("adminga so'rov yuborilmadi")


# ───────────────────────── to'lov jarayoni ─────────────────────────

async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.pre_checkout_query
    try:
        pid, uid = q.invoice_payload.split(":", 1)
        plan = PLANS.get(pid)
        ok = plan is not None and str(q.from_user.id) == uid and q.currency == "XTR" and q.total_amount == plan[2]
    except Exception:
        ok = False
    if ok:
        await q.answer(ok=True)
    else:
        await q.answer(ok=False, error_message="To'lov ma'lumotlari eskirgan. Iltimos, qaytadan urinib ko'ring.")


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    pay = msg.successful_payment
    user = update.effective_user
    charge_id = pay.telegram_payment_charge_id

    # Idempotentlik: bir xil to'lov ikki marta Pro bermasin
    try:
        if await asyncio.to_thread(db.exists, STORE, f"c:{charge_id}"):
            return
    except Exception:
        logger.exception("to'lov takrorini tekshirib bo'lmadi")

    pid = pay.invoice_payload.split(":", 1)[0]
    plan = PLANS.get(pid)
    days = plan[1] if plan else 30
    title = plan[0] if plan else "Misumi AI Pro"

    until = admin_store.grant_premium(user.id, days)

    record = {
        "user_id": user.id,
        "name": user.full_name,
        "username": user.username,
        "plan": pid,
        "days": days,
        "stars": pay.total_amount,
        "charge_id": charge_id,
        "ts": int(time.time()),
        "refunded": False,
    }
    try:
        await asyncio.to_thread(db.save, STORE, f"c:{charge_id}", record)
        log = await asyncio.to_thread(db.load, STORE, "log", [])
        log.append({"charge_id": charge_id, "user_id": user.id, "stars": pay.total_amount, "plan": pid, "ts": record["ts"]})
        await asyncio.to_thread(db.save, STORE, "log", log[-300:])
    except Exception:
        logger.exception("to'lovni bazaga yozib bo'lmadi (Pro berildi): %s", record)

    until_txt = time.strftime("%d.%m.%Y", time.gmtime(until + 5 * 3600)) if until else "cheksiz"
    await msg.reply_text(
        f"✅ To'lov qabul qilindi — rahmat!\n\n💎 {title}\n📅 Amal qilish muddati: {until_txt} gacha\n\n"
        "Endi Xizmatlar bo'limidagi Pro funksiyalarni yoqishingiz mumkin.\n"
        f"Muammo bo'lsa: /paysupport"
    )
    if ADMIN_ID:
        try:
            uname = f"@{user.username}" if user.username else user.full_name
            await context.bot.send_message(
                int(ADMIN_ID),
                f"⭐ Yangi to'lov: {pay.total_amount} Stars\n{uname} (ID: {user.id})\nTarif: {title}\n"
                f"Charge: <code>{charge_id}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass


# ───────────────────────── yordamchi buyruqlar ─────────────────────────

async def cmd_paysupport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "To'lov bilan muammo bo'ldimi? Stars yechilgan, lekin Pro yoqilmagan bo'lsa — "
        f"{misumi_info.SUPPORT} ga yozing va to'lov vaqtini/ID'ingizni ayting. "
        "Tekshirib, Pro'ni yoqamiz yoki Stars'ni qaytaramiz."
    )


async def cmd_payments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    log = await asyncio.to_thread(db.load, STORE, "log", [])
    if not log:
        await update.effective_message.reply_text("Hali to'lovlar yo'q.")
        return
    total = sum(x.get("stars", 0) for x in log)
    lines = [f"⭐ Jami (oxirgi {len(log)} ta): {total} Stars\n"]
    for x in log[-10:][::-1]:
        t = time.strftime("%d.%m %H:%M", time.gmtime(x["ts"] + 5 * 3600))
        lines.append(f"{t} · {x['user_id']} · {x['stars']}⭐ · {x['plan']}\n<code>{x['charge_id']}</code>")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_refund(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    if len(context.args or []) != 2:
        await update.effective_message.reply_text("Ishlatish: /refund <user_id> <charge_id>")
        return
    uid, charge_id = context.args
    try:
        await context.bot.refund_star_payment(user_id=int(uid), telegram_payment_charge_id=charge_id)
    except Exception as e:
        await update.effective_message.reply_text(f"❌ Qaytarib bo'lmadi: {e}")
        return
    admin_store.revoke_premium(uid)
    try:
        rec = await asyncio.to_thread(db.load, STORE, f"c:{charge_id}", None)
        if rec:
            rec["refunded"] = True
            await asyncio.to_thread(db.save, STORE, f"c:{charge_id}", rec)
    except Exception:
        pass
    await update.effective_message.reply_text(f"✅ Stars qaytarildi, {uid} dan Pro olib tashlandi.")


def register(app) -> None:
    # group=-1: eski ub:buy_* handleridan OLDIN ishlaydi va uni to'xtatadi
    app.add_handler(CallbackQueryHandler(buy_entry, pattern=r"^ub:buy_(pro|max)$"), group=-1)
    app.add_handler(CallbackQueryHandler(stars_callback, pattern=r"^pay:stars:"), group=0)
    app.add_handler(CallbackQueryHandler(manual_callback, pattern=r"^pay:manual$"), group=0)
    app.add_handler(PreCheckoutQueryHandler(precheckout), group=0)
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment), group=0)
    app.add_handler(CommandHandler("paysupport", cmd_paysupport), group=0)
    app.add_handler(CommandHandler("payments", cmd_payments), group=0)
    app.add_handler(CommandHandler("refund", cmd_refund), group=0)
