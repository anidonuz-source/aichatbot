"""
Misumi AI — duel game (🎲🎯🏀⚽🎳🎰).

Group members can challenge each other (PvP, via /duel as a reply to
someone's message) or challenge Misumi AI herself (PvE, plain /duel).

Players throw their OWN dice: after picking a game type, each human
sends the matching emoji as a normal message — Telegram itself turns
that into an animated dice roll with a server-side random result, so
it's genuinely the player's own throw (not the bot rolling for them),
and provably fair. When the opponent is Misumi AI, she rolls for
herself via send_dice.

Misumi AI acts as a live host throughout: an AI-written hype intro,
a live comment handing off between throws, a wrap-up comment on the
result, and a short AI-written "jazo" (dare) for the loser. Results
feed a persistent win/loss leaderboard (game_store.py).

Wired into bot.py via register(app).
"""
import asyncio
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import admin_store
import ai_core
import game_store
import sticker_store

# key -> (emoji, display label)
GAME_TYPES = {
    "dice": ("🎲", "Kub"),
    "dart": ("🎯", "Nishonga otish"),
    "bball": ("🏀", "Basketbol"),
    "foot": ("⚽", "Futbol"),
    "bowl": ("🎳", "Boulling"),
    "slot": ("🎰", "Slot mashina"),
}

# How long Telegram's dice animation roughly takes to settle — used only
# for the bot's own PvE throw, so its message doesn't feel instant/unfair.
DICE_DELAY = {
    "🎲": 3.5, "🎯": 3.5, "🏀": 3.0, "⚽": 3.0, "🎳": 3.5, "🎰": 4.0,
}

BOT_PLAYER_ID = "bot"  # sentinel target_id meaning "play against Misumi AI"

# duel_id -> duel state (see _start_duel)
_active_duels: dict[str, dict] = {}
# (chat_id, user_id) -> duel_id — who we're currently waiting to throw
_waiting: dict[tuple[int, int], str] = {}


def _game_type_keyboard(challenger_id: str, target_id: str) -> InlineKeyboardMarkup:
    buttons, row = [], []
    for key, (emoji, label) in GAME_TYPES.items():
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
        await _start_duel(
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
    await _start_duel(
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


async def _start_duel(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    emoji: str,
    label: str,
    p1_id: int,
    p1_name: str,
    p2_id: int | None,
    p2_name: str,
):
    """Set up duel state and prompt player 1 to throw their own dice
    (or, in PvE, prompt the human and let the bot roll right after)."""
    duel_id = uuid.uuid4().hex
    _active_duels[duel_id] = {
        "chat_id": chat_id, "emoji": emoji, "label": label,
        "p1_id": p1_id, "p1_name": p1_name, "p1_val": None,
        "p2_id": p2_id, "p2_name": p2_name, "p2_val": None,
        "turn": "p1",
    }
    _waiting[(chat_id, p1_id)] = duel_id

    try:
        intro = ai_core.generate_duel_intro(p1_name, p2_name, label)
    except Exception:
        intro = f"🔥 {p1_name} va {p2_name} — kim kuchli ekan, hoziroq bilamiz!"

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"{intro}\n\n{emoji} {p1_name}, boshlang — shu emojini o'zingiz yuboring!",
    )


async def on_player_dice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.dice:
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    key = (chat_id, user_id)
    duel_id = _waiting.get(key)
    if not duel_id:
        return  # this dice throw isn't part of any duel we're waiting on

    duel = _active_duels.get(duel_id)
    if not duel:
        _waiting.pop(key, None)
        return

    if message.dice.emoji != duel["emoji"]:
        await message.reply_text(f"Bu duelda {duel['emoji']} kerak — o'shani yuboring 🙂")
        return

    value = message.dice.value
    _waiting.pop(key, None)

    if duel["turn"] == "p1":
        duel["p1_val"] = value

        if duel["p2_id"] is None:
            # PvE — Misumi AI rolls for herself right after.
            await asyncio.sleep(1.0)
            bot_msg = await context.bot.send_dice(chat_id=chat_id, emoji=duel["emoji"])
            await asyncio.sleep(DICE_DELAY.get(duel["emoji"], 3.5))
            duel["p2_val"] = bot_msg.dice.value
            await _finish_duel(context, duel_id)
        else:
            duel["turn"] = "p2"
            _waiting[(chat_id, duel["p2_id"])] = duel_id
            try:
                comment = ai_core.generate_duel_waiting_comment(
                    duel["p1_name"], value, duel["p2_name"], duel["label"]
                )
            except Exception:
                comment = f"🎯 {duel['p1_name']}dan {value}! Endi {duel['p2_name']}, navbat sizda!"
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{comment}\n\n{duel['emoji']} {duel['p2_name']}, o'zingiz yuboring!",
            )
    else:
        duel["p2_val"] = value
        await _finish_duel(context, duel_id)


async def _finish_duel(context: ContextTypes.DEFAULT_TYPE, duel_id: str):
    duel = _active_duels.pop(duel_id, None)
    if not duel:
        return

    chat_id = duel["chat_id"]
    label = duel["label"]
    p1_id, p1_name, p1_val = duel["p1_id"], duel["p1_name"], duel["p1_val"]
    p2_id, p2_name, p2_val = duel["p2_id"], duel["p2_name"], duel["p2_val"]

    p1_id_s = str(p1_id) if p1_id else None
    p2_id_s = str(p2_id) if p2_id else None

    if p1_val == p2_val:
        game_store.record_result(p1_id_s, p1_name, p2_id_s, p2_name, draw=True)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🤝 Durang! Ikkalasi ham {p1_val} chiqardi. Yana urinib ko'ring!",
        )
        await _send_result_sticker(context, chat_id, "draw")
        return

    if p1_val > p2_val:
        winner_id, winner_name, loser_id, loser_name = p1_id_s, p1_name, p2_id_s, p2_name
    else:
        winner_id, winner_name, loser_id, loser_name = p2_id_s, p2_name, p1_id_s, p1_name

    game_store.record_result(winner_id, winner_name, loser_id, loser_name)

    try:
        result_comment = ai_core.generate_duel_result_comment(
            p1_name, p1_val, p2_name, p2_val, winner_name, label
        )
    except Exception:
        result_comment = f"🏆 {winner_name} g'olib! ({p1_name}: {p1_val} — {p2_name}: {p2_val})"
    await context.bot.send_message(chat_id=chat_id, text=result_comment)
    await _send_result_sticker(context, chat_id, "win")

    # loser_id is None only when the loser is Misumi AI herself (PvE,
    # p2_id is always None for the bot side) — a real human loser always
    # has a real Telegram id. When she's the one who lost, she performs
    # her own dare for real instead of just announcing one for someone
    # else to do.
    if loser_id is None:
        try:
            kind, dare_text = ai_core.generate_bot_dare(winner_name, label)
        except Exception:
            kind, dare_text = "joke", f"Tan olaman, {winner_name} — bugun siz kuchli edingiz! 👏"
        await context.bot.send_message(chat_id=chat_id, text=f"⚡ Men yutqazdim, jazoimni bajaraman:\n\n{dare_text}")
        await _send_result_sticker(context, chat_id, "lose")
        return

    try:
        jazo = ai_core.generate_duel_punishment(winner_name, loser_name, label)
    except Exception:
        jazo = f"{loser_name}, jazo sifatida guruhga bitta hazil ayting! 😄"
    await context.bot.send_message(chat_id=chat_id, text=f"⚡ Jazo — {loser_name}: {jazo}")
    await _send_result_sticker(context, chat_id, "lose")


async def _send_result_sticker(context: ContextTypes.DEFAULT_TYPE, chat_id: int, category: str):
    """Best-effort: send a random sticker for this game-result category,
    if any have been added via /stiker. Silently does nothing otherwise."""
    file_id = sticker_store.get_random(category)
    if file_id:
        try:
            await context.bot.send_sticker(chat_id=chat_id, sticker=file_id)
        except Exception:
            pass


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


async def stiker_add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: reply to a sticker with `/stiker <category>` to add it to
    Misumi AI's library — she'll use it herself later, in chat or in
    game results, depending on the category."""
    message = update.message
    if not message.reply_to_message or not message.reply_to_message.sticker:
        await message.reply_text(
            "Bitta stikerga shu buyruq bilan reply qiling: /stiker <turkum>\n"
            f"Turkumlar: {', '.join(sticker_store.CATEGORIES)}"
        )
        return

    if not context.args:
        await message.reply_text(f"Turkumni ko'rsating. Masalan: /stiker happy\nTurkumlar: {', '.join(sticker_store.CATEGORIES)}")
        return

    category = context.args[0].strip().lower()
    file_id = message.reply_to_message.sticker.file_id
    if sticker_store.add_sticker(category, file_id):
        await message.reply_text(f"✅ Stiker '{category}' turkumiga qo'shildi.")
    else:
        await message.reply_text(f"Noto'g'ri turkum. Turkumlar: {', '.join(sticker_store.CATEGORIES)}")


async def stiker_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    counts = sticker_store.counts()
    lines = ["📦 Stiker/GIF to'plami:"]
    for cat, n in counts.items():
        lines.append(f"• {cat}: {n} ta")
    await update.message.reply_text("\n".join(lines))


# Last time (unix seconds) any tracked message happened in a given chat.
# In-memory only — doesn't need to survive a restart. Feeds a future
# "group's been quiet, say something" feature; for now just recorded.
_last_activity: dict[int, float] = {}


def touch_activity(chat_id: int) -> None:
    _last_activity[chat_id] = time.time()


def seconds_since_activity(chat_id: int) -> float | None:
    ts = _last_activity.get(chat_id)
    return None if ts is None else time.time() - ts


def _is_reply_to_misumi(message, bot_username: str | None) -> bool:
    replied = message.reply_to_message
    if not replied or not replied.from_user or not replied.from_user.is_bot:
        return False
    return bool(bot_username) and replied.from_user.username == bot_username


async def collect_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs on every sticker any group member sends, no command needed.
    Silently sorts it into the library by emoji (unrecognized -> 'general'),
    growing the collection the more the group actually uses stickers —
    same as a real member would pick things up over time.

    If the sticker is sent as a reply to one of Misumi's own messages
    (i.e. addressed to her, the way you'd reply-sticker a friend), she
    also actually reacts to it like a real person would — text, a
    sticker/GIF of her own, or a tap reaction, via the normal AI reply
    pipeline. Otherwise stays completely silent, never errors out loud —
    a failure here must not disrupt the chat."""
    message = update.message
    if not message or not message.sticker:
        return
    chat_id = update.effective_chat.id
    if admin_store.is_blocked(chat_id):
        return
    touch_activity(chat_id)
    try:
        category = sticker_store.category_for_emoji(message.sticker.emoji)
        sticker_store.add_sticker(category, message.sticker.file_id)
    except Exception:
        pass

    bot_username = context.bot.username
    if not _is_reply_to_misumi(message, bot_username):
        return

    user = update.effective_user
    display_name = user.first_name if user else None
    prompt = f"[sticker: {message.sticker.emoji or 'no emoji'}]"
    try:
        reply_text = ai_core.get_ai_reply(chat_id, prompt, name=display_name, source="telegram")
    except Exception:
        return
    try:
        await ai_core.deliver_ai_reply(
            context.bot, chat_id, chat_id, reply_text, reply_to_message_id=message.message_id
        )
    except Exception:
        pass


async def collect_gif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Same as collect_sticker but for GIFs (Telegram calls them
    'animation'). No emoji to sort by, so they all go into one shared
    pool. Also replies through the normal AI pipeline when the GIF is
    sent as a reply to Misumi herself."""
    message = update.message
    if not message or not message.animation:
        return
    chat_id = update.effective_chat.id
    if admin_store.is_blocked(chat_id):
        return
    touch_activity(chat_id)
    try:
        sticker_store.add_gif(message.animation.file_id)
    except Exception:
        pass

    bot_username = context.bot.username
    if not _is_reply_to_misumi(message, bot_username):
        return

    user = update.effective_user
    display_name = user.first_name if user else None
    prompt = "[GIF yubordi]"
    try:
        reply_text = ai_core.get_ai_reply(chat_id, prompt, name=display_name, source="telegram")
    except Exception:
        return
    try:
        await ai_core.deliver_ai_reply(
            context.bot, chat_id, chat_id, reply_text, reply_to_message_id=message.message_id
        )
    except Exception:
        pass


def register(app: Application) -> None:
    """Call once from bot.py's main() to wire up all game handlers."""
    app.add_handler(CommandHandler("duel", duel_cmd))
    app.add_handler(CommandHandler("reyting", leaderboard_cmd))
    app.add_handler(CommandHandler("stiker", stiker_add_cmd))
    app.add_handler(CommandHandler("stikerlar", stiker_list_cmd))
    app.add_handler(CallbackQueryHandler(on_type_selected, pattern=r"^dg:t:"))
    app.add_handler(CallbackQueryHandler(on_accept, pattern=r"^dg:a:"))
    app.add_handler(CallbackQueryHandler(on_decline, pattern=r"^dg:d:"))
    # Dice messages have no .text, so this never collides with
    # handle_message's filters.TEXT handler in bot.py.
    app.add_handler(MessageHandler(filters.Dice(), on_player_dice))
    # Passive background collectors — group=1 so they run in a separate
    # handler group and never block/compete with the /stiker command or
    # anything else reacting to the same message.
    app.add_handler(MessageHandler(filters.Sticker.ALL, collect_sticker), group=1)
    app.add_handler(MessageHandler(filters.ANIMATION, collect_gif), group=1)
