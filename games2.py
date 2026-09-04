"""
Misumi AI — /tictactoe va /quiz o'yinlari.

/tictactoe — reply orqali (yoki mention bilan) ikki a'zo X-O o'ynaydi.
/quiz      — guruh viktorinasi, ball to'planadi (game_store orqali persistent).

Wired into bot.py via register(app).
"""
import random
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import game_store

# ═══════════════════════════════════════════════════════════════════════════
# 🎯 TIC-TAC-TOE
# ═══════════════════════════════════════════════════════════════════════════

# game_id -> {"board": [9], "players": {"X": (id,name), "O": (id,name)},
#             "turn": "X"|"O", "chat_id": int}
_ttt_games: dict[str, dict] = {}

TTT_WIN_LINES = [
    (0, 1, 2), (3, 4, 5), (6, 7, 8),
    (0, 3, 6), (1, 4, 7), (2, 5, 8),
    (0, 4, 8), (2, 4, 6),
]


def _ttt_check_winner(board: list) -> str | None:
    for a, b, c in TTT_WIN_LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    if all(board):
        return "draw"
    return None


def _ttt_keyboard(game_id: str, board: list) -> InlineKeyboardMarkup:
    rows = []
    for r in range(3):
        row = []
        for c in range(3):
            i = r * 3 + c
            label = board[i] if board[i] else "⬜"
            row.append(InlineKeyboardButton(label, callback_data=f"ttt:{game_id}:{i}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _ttt_status_text(game: dict) -> str:
    xn = game["players"]["X"][1]
    on = game["players"]["O"][1]
    turn_name = game["players"][game["turn"]][1]
    turn_mark = game["turn"]
    return (
        f"❌⭕ <b>X-O O'YINI</b>\n\n"
        f"❌ {xn}  vs  ⭕ {on}\n\n"
        f"👉 Navbat: <b>{turn_name}</b> ({turn_mark})"
    )


async def tictactoe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await msg.reply_text("❌ Bu o'yin faqat guruhlarda ishlaydi! 👥")
        return

    challenger = update.effective_user
    target = None
    if msg.reply_to_message and msg.reply_to_message.from_user:
        ru = msg.reply_to_message.from_user
        if not ru.is_bot and ru.id != challenger.id:
            target = ru

    if target is None:
        await msg.reply_text(
            "❓ Kimningdir xabariga reply qilib <code>/tictactoe</code> yozing — "
            "o'sha kishi bilan X-O o'ynaysiz!",
            parse_mode="HTML"
        )
        return

    game_id = uuid.uuid4().hex[:8]
    game = {
        "board": [""] * 9,
        "players": {"X": (challenger.id, challenger.first_name or "O'yinchi"),
                    "O": (target.id, target.first_name or "Raqib")},
        "turn": "X",
        "chat_id": chat.id,
    }
    _ttt_games[game_id] = game

    await msg.reply_text(
        _ttt_status_text(game),
        parse_mode="HTML",
        reply_markup=_ttt_keyboard(game_id, game["board"])
    )


async def ttt_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    _, game_id, idx_s = query.data.split(":")
    idx = int(idx_s)

    game = _ttt_games.get(game_id)
    if not game:
        await query.answer("O'yin topilmadi yoki tugagan! 😅", show_alert=True)
        return

    turn_mark = game["turn"]
    turn_uid = game["players"][turn_mark][0]
    if user.id != turn_uid:
        await query.answer("Sizning navbatingiz emas! 😅", show_alert=True)
        return
    if game["board"][idx]:
        await query.answer("Bu katak band! 😅", show_alert=True)
        return

    game["board"][idx] = turn_mark
    result = _ttt_check_winner(game["board"])

    if result is None:
        game["turn"] = "O" if turn_mark == "X" else "X"
        await query.answer()
        try:
            await query.edit_message_text(
                _ttt_status_text(game),
                parse_mode="HTML",
                reply_markup=_ttt_keyboard(game_id, game["board"])
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
        return

    # O'yin tugadi
    del _ttt_games[game_id]
    board = game["board"]
    board_text = "\n".join(
        " ".join(board[r * 3 + c] or "⬜" for c in range(3)) for r in range(3)
    )
    if result == "draw":
        text = f"🤝 <b>DURRANG!</b>\n\n{board_text}"
        game_store.record_result(
            winner_id=game["players"]["X"][0], winner_name=game["players"]["X"][1],
            loser_id=game["players"]["O"][0], loser_name=game["players"]["O"][1],
            draw=True,
        )
    else:
        winner_name = game["players"][result][1]
        winner_id = game["players"][result][0]
        loser_mark = "O" if result == "X" else "X"
        loser_id, loser_name = game["players"][loser_mark]
        game_store.record_result(winner_id, winner_name, loser_id, loser_name)
        text = f"🏆 <b>{winner_name} G'OLIB!</b> ({result})\n\n{board_text}"

    await query.answer()
    try:
        await query.edit_message_text(text, parse_mode="HTML")
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


# ═══════════════════════════════════════════════════════════════════════════
# 🧠 QUIZ
# ═══════════════════════════════════════════════════════════════════════════

QUIZ_QUESTIONS = [
    {"q": "O'zbekistonning poytaxti qaysi shahar?", "options": ["Samarqand", "Toshkent", "Buxoro", "Andijon"], "correct": 1},
    {"q": "Yer atrofida nechta sayyora aylanadi (Quyosh tizimida jami nechta sayyora bor)?", "options": ["7", "8", "9", "10"], "correct": 1},
    {"q": "Suvning kimyoviy formulasi qanday?", "options": ["CO2", "O2", "H2O", "NaCl"], "correct": 2},
    {"q": "Dunyodagi eng katta okean qaysi?", "options": ["Atlantika", "Hind okeani", "Tinch okean", "Shimoliy Muz okeani"], "correct": 2},
    {"q": "1 kilometr nechta metrga teng?", "options": ["10", "100", "1000", "10000"], "correct": 2},
    {"q": "Inson tanasida nechta suyak bor (kattalarda, taxminan)?", "options": ["106", "156", "206", "256"], "correct": 2},
    {"q": "Alisher Navoiy qaysi asrda yashagan?", "options": ["XIV asr", "XV asr", "XVI asr", "XVII asr"], "correct": 1},
    {"q": "Eng tez sayyora (Quyoshga eng yaqin) qaysi?", "options": ["Venera", "Merkuriy", "Mars", "Yer"], "correct": 1},
    {"q": "Futbolda bitta jamoada nechta o'yinchi maydonda bo'ladi?", "options": ["9", "10", "11", "12"], "correct": 2},
    {"q": "\"Bir yil\"da nechta oy bor?", "options": ["10", "11", "12", "13"], "correct": 2},
]

# chat_id -> {"question": dict, "answered_by": set(uid), "msg_id": int, "created": float}
_active_quiz: dict[int, dict] = {}
QUIZ_ANSWER_WINDOW = 20  # sekund


def _quiz_keyboard(options: list, qid: str) -> InlineKeyboardMarkup:
    rows = []
    for i, opt in enumerate(options):
        rows.append([InlineKeyboardButton(opt, callback_data=f"quiz:{qid}:{i}")])
    return InlineKeyboardMarkup(rows)


async def quiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await msg.reply_text("❌ Bu o'yin faqat guruhlarda ishlaydi! 👥")
        return

    if chat.id in _active_quiz and time.time() - _active_quiz[chat.id]["created"] < QUIZ_ANSWER_WINDOW:
        await msg.reply_text("⏳ Hozir savol faol — avval unga javob berilsin!")
        return

    question = random.choice(QUIZ_QUESTIONS)
    qid = uuid.uuid4().hex[:8]
    _active_quiz[chat.id] = {
        "qid": qid,
        "question": question,
        "answered_by": set(),
        "created": time.time(),
    }

    sent = await msg.reply_text(
        f"🧠 <b>VIKTORINA</b>\n\n❓ {question['q']}\n\n"
        f"⏱ {QUIZ_ANSWER_WINDOW} soniya ichida javob bering!",
        parse_mode="HTML",
        reply_markup=_quiz_keyboard(question["options"], qid)
    )
    _active_quiz[chat.id]["msg_id"] = sent.message_id


async def quiz_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    chat_id = query.message.chat_id
    _, qid, idx_s = query.data.split(":")
    idx = int(idx_s)

    state = _active_quiz.get(chat_id)
    if not state or state["qid"] != qid:
        await query.answer("Bu savol allaqachon tugagan! 😅", show_alert=True)
        return

    if user.id in state["answered_by"]:
        await query.answer("Siz allaqachon javob berdingiz!", show_alert=True)
        return
    state["answered_by"].add(user.id)

    correct_idx = state["question"]["correct"]
    if idx == correct_idx:
        game_store.record_result(
            winner_id=user.id, winner_name=user.first_name or "O'yinchi",
            loser_id=None, loser_name=None,
        )
        await query.answer("✅ To'g'ri! Ball qo'shildi 🎉", show_alert=True)
        # Savolni yakunlash — birinchi to'g'ri javob g'olib
        del _active_quiz[chat_id]
        try:
            await query.edit_message_text(
                f"🧠 <b>VIKTORINA TUGADI!</b>\n\n"
                f"❓ {state['question']['q']}\n"
                f"✅ To'g'ri javob: <b>{state['question']['options'][correct_idx]}</b>\n\n"
                f"🏆 G'olib: {user.first_name}",
                parse_mode="HTML"
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
    else:
        await query.answer("❌ Noto'g'ri, qaytadan urinib ko'ring boshqa safar!", show_alert=True)


def register(app: Application) -> None:
    app.add_handler(CommandHandler("tictactoe", tictactoe_cmd))
    app.add_handler(CallbackQueryHandler(ttt_cb, pattern=r"^ttt:"))
    app.add_handler(CommandHandler("quiz", quiz_cmd))
    app.add_handler(CallbackQueryHandler(quiz_cb, pattern=r"^quiz:"))
