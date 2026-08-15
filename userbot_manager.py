"""
Misumi AI — userbot manager (Telethon / MTProto layer).

Lets a bot user "AI ga akkount ulash" — connect their own personal
Telegram account so that, when they're away, Misumi AI answers their
private messages *from their own account*.

Feature tiers:
  - Auto-reply when offline/AFK — FREE, available to every connected
    account.
  - Auto-bio updates, and a customizable "away after N minutes"
    schedule — Misumi AI Pro only (see admin_store.is_premium).

Two moving parts:

1. Login flow — a short-lived Telethon client used only to walk
   through phone -> code -> (2FA password) -> session string. One
   in-memory instance per chat while the person is mid-login.

2. Runtime clients (`start_userbot` / `stop_userbot`) — long-lived
   Telethon clients, one per connected owner, that listen for incoming
   private messages and reply with AI when the owner is AFK, and
   (Pro only) periodically refresh the account bio.

"AFK / offline" is approximated the way most userbot AFK-reply tools
do it: we track the last time the *real* human sent a message from
their own device (an outgoing Telethon event). If nothing was sent
manually in the last N minutes (DEFAULT_OFFLINE_MINUTES, customizable
by Pro users), the owner is treated as away and Misumi AI is allowed
to answer for them. Free accounts always use the default.
"""
import asyncio
import logging
import os
import random
import time
from datetime import datetime, timezone

from telethon import TelegramClient, events, functions
from telethon.errors import (
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

import admin_store
import ai_core
import userbot_store

logger = logging.getLogger("misumi-userbot")

API_ID = int(os.environ.get("TELEGRAM_API_ID", "0") or "0")
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")

DEFAULT_OFFLINE_MINUTES = 3   # no manual activity for 3 min = "away" (free default)
BIO_REFRESH_SECONDS = 45 * 60  # how often auto-bio may rotate (Pro only)

# Shown once per conversation, before the very first AI auto-reply to a
# given sender. Free accounts always get it (transparency + doubles as
# organic marketing for the bot); Misumi AI Pro accounts can turn it off
# for a more natural, unbranded feel (see userbot_store "signature" setting).
SIGNATURE_NOTE = (
    "Assalomu alaykum! Hozir oflaynman, biroz vaqtdan so'ng albatta javob "
    "beraman 🙏 Hozircha o'rnimga yordamchim yozib turibdi — @MisumiAIBot 🤖\n\n"
)

DIGEST_CHECK_SECONDS = 60 * 60  # how often the digest loop wakes up to check
DIGEST_INTERVALS = {"daily": 24 * 3600, "weekly": 7 * 24 * 3600}

# owner_id (str) -> {"client": TelegramClient, "last_active": float, "task": Task}
_active: dict[str, dict] = {}

# chat_id (str) -> in-progress Telethon client for the login conversation
_pending_logins: dict[str, TelegramClient] = {}


def _require_api_creds():
    if not API_ID or not API_HASH:
        raise RuntimeError(
            "TELEGRAM_API_ID / TELEGRAM_API_HASH not set. Get them for free "
            "at https://my.telegram.org/apps and add to your env (see "
            ".env.example) — required for the userbot feature."
        )


# ---------------------------------------------------------------------------
# Login flow: phone -> code -> (2FA password) -> session string
# ---------------------------------------------------------------------------

async def start_login(chat_id, phone: str):
    """Step 1: send the Telegram login code to `phone`. Returns the
    phone_code_hash needed for sign_in."""
    _require_api_creds()
    chat_id = str(chat_id)
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    sent = await client.send_code_request(phone)
    _pending_logins[chat_id] = client
    return sent.phone_code_hash


async def submit_code(chat_id, phone: str, code: str):
    """Step 2: submit the SMS/app code. Raises SessionPasswordNeededError
    if the account has 2FA enabled (caller should then ask for the
    password and call submit_password)."""
    chat_id = str(chat_id)
    client = _pending_logins.get(chat_id)
    if not client:
        raise RuntimeError("Login session topilmadi. /start dan qayta boshlang.")
    try:
        await client.sign_in(phone=phone, code=code)
    except PhoneCodeInvalidError:
        raise ValueError("Kod noto'g'ri. Qayta urinib ko'ring.")
    return await _finish_login(chat_id, client)


async def submit_password(chat_id, password: str):
    """Step 2b: only needed if the account has Two-Step Verification."""
    chat_id = str(chat_id)
    client = _pending_logins.get(chat_id)
    if not client:
        raise RuntimeError("Login session topilmadi. /start dan qayta boshlang.")
    await client.sign_in(password=password)
    return await _finish_login(chat_id, client)


async def _finish_login(chat_id: str, client: TelegramClient) -> str:
    session_string = client.session.save()
    await client.disconnect()
    _pending_logins.pop(chat_id, None)
    return session_string


def cancel_login(chat_id) -> None:
    chat_id = str(chat_id)
    client = _pending_logins.pop(chat_id, None)
    if client:
        try:
            asyncio.get_event_loop().create_task(client.disconnect())
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Runtime: background listener + auto-bio per connected owner
# ---------------------------------------------------------------------------

def _mark_active(owner_id: str) -> None:
    entry = _active.get(owner_id)
    if entry:
        entry["last_active"] = time.time()


def _offline_minutes(owner_id: str) -> int:
    """Pro users can customize how many minutes of silence count as
    'away'; free users get the default."""
    if not admin_store.is_premium(owner_id):
        return DEFAULT_OFFLINE_MINUTES
    settings = userbot_store.get_settings(owner_id)
    return int(settings.get("offline_minutes") or DEFAULT_OFFLINE_MINUTES)


def _is_away(owner_id: str) -> bool:
    entry = _active.get(owner_id)
    if not entry:
        return False
    threshold = _offline_minutes(owner_id) * 60
    return (time.time() - entry["last_active"]) >= threshold


async def _handle_incoming(owner_id: str, event) -> None:
    # Auto-reply when offline is a FREE feature — available to every
    # connected account, no Pro check here.
    settings = userbot_store.get_settings(owner_id)
    if not settings.get("auto_reply"):
        return
    if not _is_away(owner_id):
        return  # owner is around, let them answer themselves

    sender = await event.get_sender()
    if sender is None or getattr(sender, "bot", False):
        return

    is_pro = admin_store.is_premium(owner_id)

    # Blacklist (Pro) — never auto-reply to specific people.
    if is_pro and userbot_store.is_blacklisted(owner_id, sender.id):
        return

    sender_name = getattr(sender, "first_name", None) or "Do'st"
    memory_key = f"ub_{owner_id}_{sender.id}"
    is_first_contact = memory_key not in ai_core._history

    reply_text = None
    stat_key = "auto_replies_sent"

    voice = getattr(event.message, "voice", None)
    if voice:
        if not (is_pro and settings.get("voice_reply")):
            return  # feature off/not Pro — stay silent rather than mistext
        try:
            audio_bytes = await event.download_media(bytes)
            reply_text = ai_core.get_ai_reply(
                memory_key,
                "Bu ovozli xabar. Uni tingla va tabiiy, qisqa javob yoz.",
                image_bytes=audio_bytes,
                image_mime="audio/ogg",
                name=sender_name,
                source="userbot-voice",
            )
        except Exception:
            logger.exception("Userbot voice reply error (owner=%s)", owner_id)
            return
        stat_key = "voice_replies_sent"
    else:
        text = event.raw_text or ""
        if not text.strip():
            return

        # Keyword-triggered canned replies (Pro) — instant, no AI call.
        keywords = userbot_store.get_keywords(owner_id) if is_pro else {}
        matched = next((kw for kw in keywords if kw in text.lower()), None)
        if matched:
            reply_text = keywords[matched]
            stat_key = "keyword_replies_sent"
        else:
            try:
                reply_text = ai_core.get_ai_reply(memory_key, text, name=sender_name, source="userbot")
            except Exception:
                logger.exception("Userbot AI reply error (owner=%s)", owner_id)
                return
            stat_key = "auto_replies_sent"

    # Free accounts: always prepend the signature on the first reply in a
    # conversation. Pro accounts: only if they didn't opt out.
    show_signature = is_first_contact and (not is_pro or settings.get("signature", True))
    if show_signature:
        reply_text = SIGNATURE_NOTE + reply_text

    try:
        await event.reply(reply_text)
        userbot_store.bump_stat(owner_id, stat_key)
    except Exception:
        logger.exception("Userbot send error (owner=%s)", owner_id)


async def _bio_loop(owner_id: str, client: TelegramClient) -> None:
    # Auto-bio stays a Misumi AI Pro-only feature.
    while owner_id in _active:
        try:
            await asyncio.sleep(BIO_REFRESH_SECONDS + random.randint(0, 300))
            if owner_id not in _active:
                break
            settings = userbot_store.get_settings(owner_id)
            if not settings.get("auto_bio") or not admin_store.is_premium(owner_id):
                continue
            status = "band, hozircha javob berolmaydi" if _is_away(owner_id) else "onlayn, faol"
            bio_text = ai_core._general_call(
                "Write ONE short, natural Telegram bio (max ~60 characters) "
                "reflecting the user's current status. Sound like a real "
                "person's profile bio, not a bot. Default language: Uzbek, "
                "informal. Output ONLY the bio text, no quotes.",
                f"Hozirgi holat: {status}. Shunga mos qisqa bio yoz.",
                "Band, tez orada javob beraman ✨",
            )
            bio_text = (bio_text or "").strip().strip('"')[:70]
            if not bio_text:
                continue
            await client(functions.account.UpdateProfileRequest(about=bio_text))
            userbot_store.bump_stat(owner_id, "bio_updates")
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Userbot bio loop error (owner=%s)", owner_id)


SELF_CHAT_PREFIX = ".ai"  # send ".ai <savol>" to Saved Messages to talk to Misumi AI


async def _handle_self_chat(owner_id: str, event) -> None:
    """Lets the owner manage Misumi AI privately, inside their own account,
    by sending commands to Saved Messages (self-chat). All commands below
    are Misumi AI Pro only.

      .ai <savol>            — chat with Misumi AI directly (needs the
                                separate "Ichki AI yordamchisi" toggle on)
      .kw <so'z> | <javob>   — add/update a keyword auto-reply
      .kw del <so'z>         — remove a keyword auto-reply
      .kw list               — list current keyword auto-replies
      .block <@user yoki id> — never auto-reply to this person again
      .unblock <@user yoki id> — remove from the blacklist
      .stats daily|weekly|off — periodic stats digest to Saved Messages
    """
    if not admin_store.is_premium(owner_id):
        return

    entry = _active.get(owner_id)
    if not entry or event.chat_id != entry.get("my_id"):
        return  # only reacts inside Saved Messages, not other chats

    text = (event.raw_text or "").strip()
    lower = text.lower()

    if lower.startswith(SELF_CHAT_PREFIX):
        settings = userbot_store.get_settings(owner_id)
        if not settings.get("inner_ai"):
            return
        query = text[len(SELF_CHAT_PREFIX):].strip()
        if not query:
            return
        memory_key = f"ub_self_{owner_id}"
        try:
            reply = ai_core.get_ai_reply(memory_key, query, name="Siz", source="userbot-self")
        except Exception:
            logger.exception("Userbot self-chat AI error (owner=%s)", owner_id)
            return
        try:
            await event.client.send_message(event.chat_id, f"🤖 {reply}")
            userbot_store.bump_stat(owner_id, "self_chat_replies")
        except Exception:
            logger.exception("Userbot self-chat send error (owner=%s)", owner_id)
        return

    if lower.startswith(".kw"):
        await _handle_kw_command(owner_id, event, text[3:].strip())
        return

    if lower.startswith(".block") or lower.startswith(".unblock"):
        await _handle_block_command(owner_id, event, lower.startswith(".unblock"),
                                     text.split(maxsplit=1)[1].strip() if " " in text else "")
        return

    if lower.startswith(".stats"):
        await _handle_stats_command(owner_id, event, text[6:].strip().lower())
        return


async def _handle_kw_command(owner_id: str, event, rest: str) -> None:
    if rest.lower() == "list":
        keywords = userbot_store.get_keywords(owner_id)
        if not keywords:
            await event.client.send_message(event.chat_id, "🔑 Hozircha kalit so'z qo'shilmagan.")
            return
        lines = ["🔑 Kalit so'zlar:"] + [f"• {k} → {v}" for k, v in keywords.items()]
        await event.client.send_message(event.chat_id, "\n".join(lines))
        return

    if rest.lower().startswith("del "):
        kw = rest[4:].strip()
        userbot_store.remove_keyword(owner_id, kw)
        await event.client.send_message(event.chat_id, f"🗑 O'chirildi: {kw}")
        return

    if "|" not in rest:
        await event.client.send_message(
            event.chat_id,
            "Format: .kw so'z | javob   (masalan: .kw narx | 1 oylik obuna narxi 50 000 so'm)\n"
            "Ro'yxat: .kw list\nO'chirish: .kw del so'z",
        )
        return

    kw, reply = rest.split("|", 1)
    kw, reply = kw.strip(), reply.strip()
    if not kw or not reply:
        await event.client.send_message(event.chat_id, "So'z va javob bo'sh bo'lmasligi kerak.")
        return
    userbot_store.set_keyword(owner_id, kw, reply)
    await event.client.send_message(event.chat_id, f"✅ Saqlandi: \"{kw}\" → {reply}")


async def _handle_block_command(owner_id: str, event, unblock: bool, target: str) -> None:
    user_id = None
    if not target and event.message.is_reply:
        replied = await event.message.get_reply_message()
        if replied and replied.sender_id:
            user_id = replied.sender_id
    elif target:
        try:
            entity = await event.client.get_entity(target)
            user_id = entity.id
        except Exception:
            await event.client.send_message(event.chat_id, "Foydalanuvchi topilmadi. @username yoki ID yozing.")
            return

    if user_id is None:
        await event.client.send_message(
            event.chat_id,
            "Kimni bloklash kerak? .block @username yoki .block 123456789, "
            "yoki shu odamning xabariga reply qilib .block deb yozing.",
        )
        return

    if unblock:
        userbot_store.remove_blacklist(owner_id, user_id)
        await event.client.send_message(event.chat_id, f"✅ Blokdan chiqarildi (ID: {user_id}).")
    else:
        userbot_store.add_blacklist(owner_id, user_id)
        await event.client.send_message(event.chat_id, f"🚫 Bloklandi (ID: {user_id}) — endi avto-javob yozilmaydi.")


async def _handle_stats_command(owner_id: str, event, choice: str) -> None:
    if choice not in ("daily", "weekly", "off"):
        await event.client.send_message(event.chat_id, "Format: .stats daily / .stats weekly / .stats off")
        return
    value = None if choice == "off" else choice
    userbot_store.set_setting(owner_id, "stats_digest", value)
    if value:
        userbot_store.set_last_digest_at(owner_id, datetime.now(timezone.utc).isoformat())
        label = "har kuni" if value == "daily" else "har hafta"
        await event.client.send_message(event.chat_id, f"📊 Statistika hisoboti yoqildi — {label} shu yerga yuboriladi.")
    else:
        await event.client.send_message(event.chat_id, "📊 Statistika hisoboti o'chirildi.")


async def _digest_loop(owner_id: str, client: TelegramClient) -> None:
    """Pro feature: periodically post a stats summary to the owner's own
    Saved Messages (daily or weekly, per userbot_store "stats_digest")."""
    while owner_id in _active:
        try:
            await asyncio.sleep(DIGEST_CHECK_SECONDS)
            if owner_id not in _active:
                break
            if not admin_store.is_premium(owner_id):
                continue
            settings = userbot_store.get_settings(owner_id)
            freq = settings.get("stats_digest")
            if freq not in DIGEST_INTERVALS:
                continue

            last_at = userbot_store.get_last_digest_at(owner_id)
            now = datetime.now(timezone.utc)
            if last_at:
                try:
                    elapsed = (now - datetime.fromisoformat(last_at)).total_seconds()
                except ValueError:
                    elapsed = DIGEST_INTERVALS[freq]
            else:
                elapsed = DIGEST_INTERVALS[freq]
            if elapsed < DIGEST_INTERVALS[freq]:
                continue

            stats = userbot_store.get_stats(owner_id)
            label = "Kunlik" if freq == "daily" else "Haftalik"
            text = (
                f"📊 {label} hisobot (Misumi AI)\n\n"
                f"🤖 Oflayn avto-javoblar: {stats.get('auto_replies_sent', 0)}\n"
                f"🔑 Kalit so'z javoblari: {stats.get('keyword_replies_sent', 0)}\n"
                f"🎙 Ovozli xabar javoblari: {stats.get('voice_replies_sent', 0)}\n"
                f"📝 Bio yangilanishlari: {stats.get('bio_updates', 0)}\n"
                f"🧠 Ichki AI suhbatlari: {stats.get('self_chat_replies', 0)}\n\n"
                f"(Ulanganingizdan beri jami)"
            )
            await client.send_message("me", text)
            userbot_store.set_last_digest_at(owner_id, now.isoformat())
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Userbot digest loop error (owner=%s)", owner_id)


async def start_userbot(owner_id) -> bool:
    """Starts (or restarts) the background client for an already-connected
    owner. Safe to call more than once — no-ops if already running."""
    _require_api_creds()
    owner_id = str(owner_id)
    if owner_id in _active:
        return True

    session_string = userbot_store.get_session(owner_id)
    if not session_string:
        return False

    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        logger.warning("Userbot session invalid/expired for owner=%s", owner_id)
        await client.disconnect()
        return False

    me = await client.get_me()
    _active[owner_id] = {"client": client, "last_active": time.time(), "tasks": [], "my_id": me.id}

    @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def _on_incoming(event, _owner_id=owner_id):
        await _handle_incoming(_owner_id, event)

    @client.on(events.NewMessage(outgoing=True))
    async def _on_outgoing(event, _owner_id=owner_id):
        _mark_active(_owner_id)  # real human is actively using the account
        await _handle_self_chat(_owner_id, event)

    loop = asyncio.get_event_loop()
    _active[owner_id]["tasks"] = [
        loop.create_task(_bio_loop(owner_id, client)),
        loop.create_task(_digest_loop(owner_id, client)),
    ]
    logger.info("Userbot started for owner=%s", owner_id)
    return True


async def stop_userbot(owner_id) -> None:
    owner_id = str(owner_id)
    entry = _active.pop(owner_id, None)
    if not entry:
        return
    if entry.get("tasks"):
        for task in entry["tasks"]:
            task.cancel()
    try:
        await entry["client"].disconnect()
    except Exception:
        pass
    logger.info("Userbot stopped for owner=%s", owner_id)


async def resume_all() -> None:
    """Call once on process startup to reconnect every previously-linked
    account (survives redeploys/restarts)."""
    if not API_ID or not API_HASH:
        logger.info("Skipping userbot resume — TELEGRAM_API_ID/HASH not set.")
        return
    for owner_id in userbot_store.get_all_connected_owner_ids():
        try:
            await start_userbot(owner_id)
        except Exception:
            logger.exception("Failed to resume userbot for owner=%s", owner_id)
