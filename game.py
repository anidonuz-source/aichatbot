"""
Misumi AI — duel game (🎲🎯🏀⚽🎳🎰).

Group members can challenge each other (PvP, via /duel as a reply to
someone's message) or challenge Misumi AI herself (PvE, plain /duel).
Uses Telegram's native send_dice — the animated emoji whose numeric
result is generated server-side by Telegram itself, so it's provably
fair for both sides. Whoever rolls higher wins; the loser gets a
short, funny "jazo" (dare) written by the AI. Results feed a
persistent win/loss leaderboard (game_store.py).

Wired into bot.py via register(app).
"""
import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import admin_store
import ai_core
import game_store

# key -> (emoji, display label)
GAME_TYPES = {
    "dice": ("🎲", "Kub"),
    "dart": ("🎯", "Nishonga otish"),
    "bball": ("🏀", "Basketbol"),
    "foot": ("⚽", "Futbol"),
    "bowl": ("🎳", "Boulling"),
    "slot": ("🎰", "Slot mashina"),
}

# Telegram's dice animations run for roughly this many seconds before
# settling on their final value — wait this long before the next roll /
# the result message so it doesn't spoil or overlap the animation.
DICE_DELAY = {
    "🎲": 3.5,
    "🎯": 3.5,
    "🏀": 3.0,
    "⚽": 3.0,
    "🎳": 3.5,
    "🎰": 4.0,
}

BOT_PLAYER_ID = "bot"  # sentinel target_id meaning "play against Misumi AI"


def _game_type_keyboard(challenger_id: str, target_id: str) -> InlineKeyboardMarkup:
    buttons, row = [], []
    for i, (key, (emoji, label)) in enumerate(GAME_TYPES.items()):
        row.append(InlineKeyboardButton(f"{emoji} {label}", callback_data=f"dg:t:{challenger_id}:{target_id}:{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


async def duel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat_id = update.effective_chat.id
    if admin_store.is_blocked(chat_id):
        return
    if admin_store.is_maintenance():
        await message.reply_text("🛠 Hozir texnik ishlar tufayli o'yin vaqtincha ishlamayapti.")
        return

    challenger = update.effective_user
    challenger_id = str(challenger.id)
    challenger_name = challenger.first_name or "O'yinchi"

    target = None
    if message.reply_to_message and message.reply_to_message.from_user:
        ru = message.reply_to_message.from_user
        if not ru.is_bot and str(ru.id) != challenger_id:
            target = ru

    if target is None:
        keyboard = _game_type_keyboard(challenger_id, BOT_PLAYER_ID)
        await message.reply_text(
            f"🎮 {challenger_name}, {ai_core.BOT_NAME} bilan qaysi o'yinda kuch sinaysiz?",
            reply_markup=keyboard,
        )
        return

    target_id = str(target.id)
    target_name = target.first_name or "Raqib"
    keyboard = _game_type_keyboard(challenger_id, target_id)
    await message.reply_text(
        f"⚔️ {challenger_name} {target_name}ni duelga chaqirmoqda! O'yin turini tanlang:",
        reply_markup=keyboard,
    )


async def on_type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, _, challenger_id, target_id, key = query.data.split(":")
    user = query.from_user

    if str(user.id) != challenger_id:
        await query.answer("Bu tugma sizga tegishli emas.", show_alert=True)
        return
    await query.answer()

    emoji, label = GAME_TYPES[key]
    challenger_name = user.first_name or "O'yinchi"

    if target_id == BOT_PLAYER_ID:
        await query.edit_message_text(f"🎮 {challenger_name} vs {ai_core.BOT_NAME} — {label} {emoji}")
        await _run_duel(
            context, query.message.chat_id, emoji, label,
            int(challenger_id), challenger_name,
            None, ai_core.BOT_NAME,
        )
        return

    target_name = "Raqib"
    try:
        cm = await context.bot.get_chat_member(query.message.chat_id, int(target_id))
        target_name = cm.user.first_name or target_name
    except Exception:
        pass

    accept_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Qabul qilish", callback_data=f"dg:a:{challenger_id}:{target_id}:{key}"),
        InlineKeyboardButton("❌ Rad etish", callback_data=f"dg:d:{challenger_id}:{target_id}"),
    ]])
    await query.edit_message_text(
        f"⚔️ {challenger_name} {target_name}ni {label} {emoji} o'yiniga chaqirmoqda!\n"
        f"{target_name}, qabul qilasizmi?",
        reply_markup=accept_kb,
    )


async def on_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, _, challenger_id, target_id, key = query.data.split(":")
    user = query.from_user

    if str(user.id) != target_id:
        await query.answer("Bu taklif sizga emas.", show_alert=True)
        return
    await query.answer()

    emoji, label = GAME_TYPES[key]
    target_name = user.first_name or "Raqib"

    challenger_name = "O'yinchi"
    try:
        cm = await context.bot.get_chat_member(query.message.chat_id, int(challenger_id))
        challenger_name = cm.user.first_name or challenger_name
    except Exception:
        pass

    await query.edit_message_text(f"✅ Duel boshlandi: {challenger_name} vs {target_name} — {label} {emoji}")
    await _run_duel(
        context, query.message.chat_id, emoji, label,
        int(challenger_id), challenger_name,
        int(target_id), target_name,
    )


async def on_decline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, _, challenger_id, target_id = query.data.split(":")
    user = query.from_user

    if str(user.id) != target_id:
        await query.answer("Bu taklif sizga emas.", show_alert=True)
        return
    await query.answer()
    await query.edit_message_text("❌ Taklif rad etildi.")


async def _run_duel(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    emoji: str,
    label: str,
    p1_id: int,
    p1_name: str,
    p2_id: int | None,
    p2_name: str,
):
    """Roll for both sides via Telegram's native dice (server-side random,
    fair for both players), announce the winner, and post an AI-written
    punishment for the loser. p2_id=None means the opponent is the bot."""
    delay = DICE_DELAY.get(emoji, 3.5)

    p1_msg = await context.bot.send_dice(chat_id=chat_id, emoji=emoji)
    p1_val = p1_msg.dice.value
    await asyncio.sleep(delay)

    p2_msg = await context.bot.send_dice(chat_id=chat_id, emoji=emoji)
    p2_val = p2_msg.dice.value
    await asyncio.sleep(delay)

    p1_id_s = str(p1_id) if p1_id else None
    p2_id_s = str(p2_id) if p2_id else None

    if p1_val == p2_val:
        game_store.record_result(p1_id_s, p1_name, p2_id_s, p2_name, draw=True)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🤝 Durang! Ikkalasi ham {p1_val} chiqardi. Yana urinib ko'ring!",
        )
        return

    if p1_val > p2_val:
        winner_id, winner_name, loser_id, loser_name = p1_id_s, p1_name, p2_id_s, p2_name
    else:
        winner_id, winner_name, loser_id, loser_name = p2_id_s, p2_name, p1_id_s, p1_name

    game_store.record_result(winner_id, winner_name, loser_id, loser_name)

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🏆 {winner_name} g'olib bo'ldi! ({p1_name}: {p1_val} — {p2_name}: {p2_val})",
    )

    try:
        jazo = ai_core.generate_duel_punishment(winner_name, loser_name, label)
    except Exception:
        jazo = f"{loser_name}, jazo sifatida guruhga bitta hazil ayting! 😄"

    await context.bot.send_message(chat_id=chat_id, text=f"⚡ Jazo — {loser_name}: {jazo}")


async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top = game_store.get_leaderboard(10)
    if not top:
        await update.message.reply_text("Hali hech kim o'ynamagan. /duel bilan boshlang!")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 Reyting (G — g'alaba, M — mag'lubiyat, D — durang):"]
    for i, u in enumerate(top):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{prefix} {u['name']} — {u['wins']}G/{u['losses']}M/{u['draws']}D")
    await update.message.reply_text("\n".join(lines))


def register(app: Application) -> None:
    """Call once from bot.py's main() to wire up all game handlers."""
    app.add_handler(CommandHandler("duel", duel_cmd))
    app.add_handler(CommandHandler("reyting", leaderboard_cmd))
    app.add_handler(CallbackQueryHandler(on_type_selected, pattern=r"^dg:t:"))
    app.add_handler(CallbackQueryHandler(on_accept, pattern=r"^dg:a:"))
    app.add_handler(CallbackQueryHandler(on_decline, pattern=r"^dg:d:"))
