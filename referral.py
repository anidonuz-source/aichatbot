"""
Misumi AI — referral (do'st taklif qilish) tizimi.

Havola:  https://t.me/<bot>?start=ref_<taklif_qiluvchi_id>
Yangi foydalanuvchi shu havola orqali /start bossa:
  • taklif qiluvchi  -> REF_INVITER_DAYS kun Pro
  • taklif qilingan  -> REF_INVITEE_DAYS kun Pro
Qoidalar (suiiste'molga qarshi):
  • faqat YANGI foydalanuvchi (botda avval yozmagan) hisoblanadi
  • o'zini o'zi taklif qila olmaydi
  • bitta odam faqat bir marta "taklif qilingan" bo'la oladi
  • bitta odam ko'pi bilan REF_MAX_REWARDS ta taklif uchun mukofot oladi

Env (ixtiyoriy): REF_INVITER_DAYS=3  REF_INVITEE_DAYS=1  REF_MAX_REWARDS=30
"""
import asyncio
import logging
import os

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

import admin_store
import db

logger = logging.getLogger("misumi-referral")
STORE = "referral"


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


INVITER_DAYS = _int_env("REF_INVITER_DAYS", 3)
INVITEE_DAYS = _int_env("REF_INVITEE_DAYS", 1)
MAX_REWARDS = _int_env("REF_MAX_REWARDS", 30)


def _is_new_user(uid: str) -> bool:
    try:
        return uid not in (admin_store._load().get("users") or {})
    except Exception:
        return False  # bazani o'qib bo'lmasa — xavfsiz tomon: mukofot bermaymiz


def _process_start(inviter: str, invitee: str) -> tuple[bool, int]:
    """Sinxron (db bilan ishlaydi). (mukofot_berildimi, taklif_qiluvchining_jami_soni)."""
    if inviter == invitee:
        return False, 0
    if db.exists(STORE, f"in:{invitee}"):
        return False, 0
    if not _is_new_user(invitee):
        return False, 0
    inv = db.load(STORE, f"out:{inviter}", {"count": 0, "rewarded": 0})
    if inv.get("rewarded", 0) >= MAX_REWARDS:
        return False, inv.get("count", 0)

    db.save(STORE, f"in:{invitee}", {"by": inviter})
    inv["count"] = inv.get("count", 0) + 1
    inv["rewarded"] = inv.get("rewarded", 0) + 1
    db.save(STORE, f"out:{inviter}", inv)

    if INVITER_DAYS:
        admin_store.grant_premium(inviter, INVITER_DAYS)
    if INVITEE_DAYS:
        admin_store.grant_premium(invitee, INVITEE_DAYS)
    return True, inv["count"]


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start ref_<id> ni ushlaydi. Hech narsani to'xtatmaydi — oddiy /start davom etadi."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or update.effective_chat.type != "private":
        return
    arg = (context.args[0] if context.args else "") or ""
    if not arg.startswith("ref_"):
        return
    inviter = arg[4:]
    if not inviter.isdigit():
        return
    try:
        ok, count = await asyncio.to_thread(_process_start, inviter, str(user.id))
    except Exception:
        logger.exception("referral ishlamadi")
        return
    if not ok:
        return
    if INVITEE_DAYS:
        try:
            await msg.reply_text(f"🎁 Do'sting taklifi bilan keldingiz — sizga {INVITEE_DAYS} kunlik Misumi AI Pro sovg'a!")
        except Exception:
            pass
    try:
        name = user.first_name or "Do'stingiz"
        await context.bot.send_message(
            int(inviter),
            f"🎉 {name} sizning havolangiz orqali qo'shildi!\n"
            f"💎 +{INVITER_DAYS} kun Pro berildi. Jami takliflar: {count}",
        )
    except Exception:
        pass  # inviter botni bloklagan bo'lishi mumkin


async def cmd_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    stats = await asyncio.to_thread(db.load, STORE, f"out:{user.id}", {"count": 0, "rewarded": 0})
    link = f"https://t.me/{context.bot.username}?start=ref_{user.id}"
    await update.effective_message.reply_text(
        "🤝 <b>Do'stlarni taklif qiling — Pro oling</b>\n\n"
        f"Har bir yangi do'st uchun sizga <b>+{INVITER_DAYS} kun Pro</b>, "
        f"do'stingizga <b>+{INVITEE_DAYS} kun Pro</b>.\n\n"
        f"🔗 Sizning havolangiz:\n<code>{link}</code>\n\n"
        f"👥 Taklif qilinganlar: <b>{stats.get('count', 0)}</b> "
        f"(mukofot limiti: {MAX_REWARDS} ta)",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


def register(app) -> None:
    # group=-3: boshqa hamma /start handlerlaridan oldin, lekin ularni to'xtatmaydi
    app.add_handler(CommandHandler("start", on_start), group=-3)
    app.add_handler(CommandHandler(["referral", "taklif", "invite"], cmd_referral), group=0)
