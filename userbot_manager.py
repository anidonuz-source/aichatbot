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
    sender_name = getattr(sender, "first_name", None) or "Do'st"
    text = event.raw_text or ""
    if not text.strip():
        return

    memory_key = f"ub_{owner_id}_{sender.id}"
    try:
        reply = ai_core.get_ai_reply(memory_key, text, name=sender_name, source="userbot")
    except Exception:
        logger.exception("Userbot AI reply error (owner=%s)", owner_id)
        return

    try:
        await event.reply(reply)
        userbot_store.bump_stat(owner_id, "auto_replies_sent")
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
    """Lets the owner chat with Misumi AI privately, inside their own
    account, by sending '.ai <question>' to Saved Messages (self-chat).
    Misumi AI Pro feature."""
    settings = userbot_store.get_settings(owner_id)
    if not settings.get("inner_ai"):
        return
    if not admin_store.is_premium(owner_id):
        return

    entry = _active.get(owner_id)
    if not entry or event.chat_id != entry.get("my_id"):
        return  # only reacts inside Saved Messages, not other chats

    text = (event.raw_text or "").strip()
    if not text.lower().startswith(SELF_CHAT_PREFIX):
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
    _active[owner_id] = {"client": client, "last_active": time.time(), "task": None, "my_id": me.id}

    @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def _on_incoming(event, _owner_id=owner_id):
        await _handle_incoming(_owner_id, event)

    @client.on(events.NewMessage(outgoing=True))
    async def _on_outgoing(event, _owner_id=owner_id):
        _mark_active(_owner_id)  # real human is actively using the account
        await _handle_self_chat(_owner_id, event)

    _active[owner_id]["task"] = asyncio.get_event_loop().create_task(
        _bio_loop(owner_id, client)
    )
    logger.info("Userbot started for owner=%s", owner_id)
    return True


async def stop_userbot(owner_id) -> None:
    owner_id = str(owner_id)
    entry = _active.pop(owner_id, None)
    if not entry:
        return
    if entry.get("task"):
        entry["task"].cancel()
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
