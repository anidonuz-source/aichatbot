"""
Misumi AI — inline rejim:  istalgan chatda  @bot_username savolingiz

Telegram inline so'rovga ~10 soniyada javob talab qiladi, AI esa sekinroq. Shuning uchun:
  1) foydalanuvchi natijani tanlaydi -> chatga "⏳ Misumi javob yozmoqda…" xabari tushadi
  2) javob tayyor bo'lgach, xabar o'zi tahrirlanib AI javobiga almashadi

Ikki yo'l bor (ikkalasi ham ishlaydi):
  • avtomatik — BotFather'da /setinlinefeedback ni 100% qiling (chosen_inline_result keladi)
  • qo'lda   — yoqilmagan bo'lsa, xabardagi "💬 Javobni olish" tugmasi bosilganda javob yoziladi

BotFather sozlamasi (bir marta):
  /setinline  -> bot tanlang -> placeholder: "Savol yozing…"
  /setinlinefeedback -> Enabled (100%)
"""
import asyncio
import logging
import time
import uuid

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Update,
)
from telegram.ext import (
    CallbackQueryHandler,
    ChosenInlineResultHandler,
    ContextTypes,
    InlineQueryHandler,
)

import ai_core

logger = logging.getLogger("misumi-inline")

MAX_Q = 400          # savol uzunligi
MAX_ANSWER = 3800    # Telegram xabar limiti 4096
_PENDING: dict[str, dict] = {}   # id -> {"q", "uid", "name", "state", "ts"}
_MAX_PENDING = 1000

_INLINE_STYLE = (
    "Bu javob boshqa odamlarga ham ko'rinadigan chatga yuboriladi. "
    "Qisqa, aniq va foydali yoz (maksimum ~900 belgi). Markdown belgilaridan (*, #, `) foydalanma. "
    "Foydalanuvchi qaysi tilda yozgan bo'lsa, shu tilda javob ber."
)


def _gc() -> None:
    if len(_PENDING) <= _MAX_PENDING:
        return
    for k, _ in sorted(_PENDING.items(), key=lambda kv: kv[1]["ts"])[: _MAX_PENDING // 2]:
        _PENDING.pop(k, None)


def _wait_kb(qid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("💬 Javobni olish", callback_data=f"iq:{qid}")]])


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    iq = update.inline_query
    q = (iq.query or "").strip()
    if not q:
        results = [
            InlineQueryResultArticle(
                id="hint",
                title="✦ Misumi AI",
                description="Savolingizni yozing — javob shu chatga yuboriladi",
                input_message_content=InputTextMessageContent(
                    f"✦ Misumi AI — savol berish uchun yozing: @{context.bot.username} savolingiz"
                ),
            )
        ]
        await iq.answer(results, cache_time=5, is_personal=True)
        return

    q = q[:MAX_Q]
    qid = uuid.uuid4().hex[:12]
    _PENDING[qid] = {
        "q": q,
        "uid": str(iq.from_user.id),
        "name": iq.from_user.first_name,
        "state": "new",
        "ts": time.time(),
    }
    _gc()
    results = [
        InlineQueryResultArticle(
            id=qid,
            title=f"🤖 Misumi AI: {q[:50]}",
            description="Bosing — AI javobi shu chatga yuboriladi",
            input_message_content=InputTextMessageContent(f"❓ {q}\n\n⏳ Misumi javob yozmoqda…"),
            reply_markup=_wait_kb(qid),
        )
    ]
    await iq.answer(results, cache_time=0, is_personal=True)


async def _generate_and_edit(context: ContextTypes.DEFAULT_TYPE, qid: str, inline_message_id: str) -> None:
    rec = _PENDING.get(qid)
    if not rec or rec["state"] != "new":
        return
    rec["state"] = "running"
    uid, q = rec["uid"], rec["q"]

    if not ai_core.check_rate_limit("inline", uid):
        rec["state"] = "new"  # tugma orqali qayta urinsa bo'ladi
        text = f"❓ {q}\n\n⏳ Biroz sekinroq — bir necha soniyadan keyin tugmani bosing."
        await _safe_edit(context, inline_message_id, text, _wait_kb(qid))
        return

    try:
        model = ai_core.resolve_model(uid, None)
        reply = await asyncio.to_thread(
            ai_core.get_ai_reply,
            uid, q,
            name=rec["name"], source="inline", model=model,
            extra_system=_INLINE_STYLE, use_memory=False,
        )
        reply = (reply or "").strip() or "Javob topa olmadim, qaytadan urinib ko'ring."
        rec["state"] = "done"
        text = f"❓ {q}\n\n🤖 {reply}"[:MAX_ANSWER]
        text += f"\n\n— @{context.bot.username}"
        await _safe_edit(context, inline_message_id, text, None)
    except Exception:
        logger.exception("inline javob xatosi")
        rec["state"] = "new"
        await _safe_edit(context, inline_message_id, f"❓ {q}\n\n⚠️ Xatolik. Qayta urinish uchun tugmani bosing.", _wait_kb(qid))


async def _safe_edit(context, inline_message_id: str, text: str, kb) -> None:
    try:
        await context.bot.edit_message_text(
            text=text, inline_message_id=inline_message_id, reply_markup=kb, disable_web_page_preview=True
        )
    except Exception as e:
        if "not modified" not in str(e).lower():
            logger.warning("inline xabarni tahrirlab bo'lmadi: %s", e)


async def chosen_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Avtomatik yo'l (inline feedback yoqilgan bo'lsa)."""
    cr = update.chosen_inline_result
    if cr.inline_message_id and cr.result_id in _PENDING:
        await _generate_and_edit(context, cr.result_id, cr.inline_message_id)


async def answer_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Qo'lda yo'l — "💬 Javobni olish" tugmasi."""
    query = update.callback_query
    qid = query.data.split(":", 1)[1]
    rec = _PENDING.get(qid)
    if not rec:
        await query.answer("Bu so'rov eskirgan. Qaytadan yozing.", show_alert=True)
        return
    if not query.inline_message_id:
        await query.answer()
        return
    if rec["state"] == "running":
        await query.answer("Javob yozilmoqda…")
        return
    if rec["state"] == "done":
        await query.answer("Javob allaqachon yozilgan ✅")
        return
    await query.answer("Javob yozilmoqda…")
    await _generate_and_edit(context, qid, query.inline_message_id)


def register(app) -> None:
    app.add_handler(InlineQueryHandler(inline_query), group=0)
    app.add_handler(ChosenInlineResultHandler(chosen_result), group=0)
    app.add_handler(CallbackQueryHandler(answer_button, pattern=r"^iq:"), group=0)
