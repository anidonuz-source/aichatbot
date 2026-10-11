"""
Misumi AI — userbot data store.

Stores, per bot-user (the person who connected their personal Telegram
account), an ENCRYPTED Telethon session string plus their feature
settings (auto-reply when offline, auto-bio) and lightweight stats.

Session strings grant full access to a Telegram account, so they are
never stored in plaintext. Encryption uses Fernet (AES128-CBC + HMAC)
with a key read from the USERBOT_ENC_KEY env var. If that var is not
set, a key is generated and saved in Postgres (db.py), so sessions survive
redeploys. Xavfsizroq: USERBOT_ENC_KEY ni Render env'ga o'zingiz qo'ying,
shunda kalit va shifrlangan sessiya turli joyda turadi.

Ma'lumot Postgres'da (Render diski vaqtinchalik). Eski _userbot_data.json va
_userbot.key fayllari bo'lsa, birinchi ishga tushishda bazaga ko'chiriladi.
"""
import functools
import os
from datetime import datetime
from pathlib import Path
from threading import RLock

from cryptography.fernet import Fernet

import db

STORE_DIR = Path(os.environ.get("MEMORY_DIR", "memory"))
STORE_DIR.mkdir(parents=True, exist_ok=True)
LEGACY_FILE = STORE_DIR / "_userbot_data.json"
KEY_FILE = STORE_DIR / "_userbot.key"
_STORE, _KEY = "userbotstore", "main"

_lock = RLock()


def _load_key() -> bytes:
    env_key = os.environ.get("USERBOT_ENC_KEY", "").strip()
    if env_key:
        return env_key.encode("utf-8")
    # 1) bazadagi kalit
    stored = db.load("userbotstore", "enckey", None)
    if isinstance(stored, str) and stored.strip():
        return stored.strip().encode("utf-8")
    # 2) eski fayldagi kalit (bazaga ko'chiramiz, aks holda eski sessiyalar ochilmay qoladi)
    if KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip().encode("utf-8")
    else:
        key = Fernet.generate_key()
    db.save("userbotstore", "enckey", key.decode("utf-8"))
    print(
        "[UserbotStore] USERBOT_ENC_KEY berilmagan: kalit bazada saqlandi. "
        "Xavfsizroq bo'lishi uchun uni Render env'ga qo'ying."
    )
    return key


_fernet = Fernet(_load_key())


def _encrypt(text: str) -> str:
    return _fernet.encrypt(text.encode("utf-8")).decode("utf-8")


def _decrypt(token: str) -> str:
    return _fernet.decrypt(token.encode("utf-8")).decode("utf-8")


def _empty() -> dict:
    return {"accounts": {}}


def _load() -> dict:
    data = db.load_migrating(_STORE, _KEY, LEGACY_FILE, None)
    if not isinstance(data, dict) or "accounts" not in data:
        return _empty()
    return data


def _save(data: dict) -> None:
    db.save(_STORE, _KEY, data)


def _default_account(phone: str) -> dict:
    return {
        "phone": phone,
        "session": None,
        "connected_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "settings": {
            "auto_reply": False,
            "auto_bio": False,
            "inner_ai": False,
            "offline_minutes": None,
            "signature": True,
            "keywords": {},       # {"narx": "1 oylik obuna narxi 50 000 so'm"} — Pro
            "blacklist": [],      # [user_id, ...] — never auto-reply to these — Pro
            "stats_digest": None, # None | "daily" | "weekly" — Pro
            "voice_reply": False, # transcribe + answer voice notes — Pro
        },
        "stats": {"auto_replies_sent": 0, "bio_updates": 0, "self_chat_replies": 0, "keyword_replies_sent": 0, "voice_replies_sent": 0},
        "last_digest_at": None,
    }


def save_session(owner_id, phone: str, session_string: str) -> None:
    owner_id = str(owner_id)
    data = _load()
    account = data["accounts"].get(owner_id) or _default_account(phone)
    account["phone"] = phone
    account["session"] = _encrypt(session_string)
    account["connected_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    data["accounts"][owner_id] = account
    _save(data)


def get_session(owner_id) -> str | None:
    account = _load()["accounts"].get(str(owner_id))
    if not account or not account.get("session"):
        return None
    try:
        return _decrypt(account["session"])
    except Exception as e:
        print(f"[UserbotStore] Decrypt error for {owner_id}: {e}")
        return None


def is_connected(owner_id) -> bool:
    return get_session(owner_id) is not None


def get_account_meta(owner_id) -> dict | None:
    return _load()["accounts"].get(str(owner_id))


def disconnect(owner_id) -> None:
    owner_id = str(owner_id)
    data = _load()
    if owner_id in data["accounts"]:
        del data["accounts"][owner_id]
        _save(data)


def get_settings(owner_id) -> dict:
    account = _load()["accounts"].get(str(owner_id))
    if not account:
        return {"auto_reply": False, "auto_bio": False}
    return account.get("settings", {"auto_reply": False, "auto_bio": False})


def set_setting(owner_id, key: str, value) -> dict:
    owner_id = str(owner_id)
    data = _load()
    account = data["accounts"].get(owner_id)
    if not account:
        return {"auto_reply": False, "auto_bio": False}
    account.setdefault("settings", {})[key] = value
    data["accounts"][owner_id] = account
    _save(data)
    return account["settings"]


def get_stats(owner_id) -> dict:
    account = _load()["accounts"].get(str(owner_id))
    if not account:
        return {"auto_replies_sent": 0, "bio_updates": 0}
    return account.get("stats", {"auto_replies_sent": 0, "bio_updates": 0})


def bump_stat(owner_id, key: str, by: int = 1) -> None:
    owner_id = str(owner_id)
    data = _load()
    account = data["accounts"].get(owner_id)
    if not account:
        return
    stats = account.setdefault("stats", {})
    stats[key] = stats.get(key, 0) + by
    data["accounts"][owner_id] = account
    _save(data)


def get_all_connected_owner_ids() -> list[str]:
    """Used on startup to resume userbot clients for everyone already
    connected before the process restarted."""
    return [
        owner_id
        for owner_id, acc in _load()["accounts"].items()
        if acc.get("session")
    ]


# ---------------------------------------------------------------------------
# Keyword-triggered canned replies (Pro) — e.g. "narx" -> fixed answer,
# sent instantly instead of calling the AI.
# ---------------------------------------------------------------------------

def get_keywords(owner_id) -> dict:
    account = _load()["accounts"].get(str(owner_id))
    if not account:
        return {}
    return account.get("settings", {}).get("keywords", {})


def set_keyword(owner_id, keyword: str, reply: str) -> dict:
    owner_id = str(owner_id)
    keyword = keyword.strip().lower()
    data = _load()
    account = data["accounts"].get(owner_id)
    if not account:
        return {}
    keywords = account.setdefault("settings", {}).setdefault("keywords", {})
    keywords[keyword] = reply.strip()
    data["accounts"][owner_id] = account
    _save(data)
    return keywords


def remove_keyword(owner_id, keyword: str) -> dict:
    owner_id = str(owner_id)
    keyword = keyword.strip().lower()
    data = _load()
    account = data["accounts"].get(owner_id)
    if not account:
        return {}
    keywords = account.setdefault("settings", {}).setdefault("keywords", {})
    keywords.pop(keyword, None)
    data["accounts"][owner_id] = account
    _save(data)
    return keywords


# ---------------------------------------------------------------------------
# Blacklist (Pro) — never auto-reply to these Telegram user ids.
# ---------------------------------------------------------------------------

def get_blacklist(owner_id) -> list[int]:
    account = _load()["accounts"].get(str(owner_id))
    if not account:
        return []
    return account.get("settings", {}).get("blacklist", [])


def add_blacklist(owner_id, user_id: int) -> list[int]:
    owner_id = str(owner_id)
    data = _load()
    account = data["accounts"].get(owner_id)
    if not account:
        return []
    blacklist = account.setdefault("settings", {}).setdefault("blacklist", [])
    if user_id not in blacklist:
        blacklist.append(user_id)
    data["accounts"][owner_id] = account
    _save(data)
    return blacklist


def remove_blacklist(owner_id, user_id: int) -> list[int]:
    owner_id = str(owner_id)
    data = _load()
    account = data["accounts"].get(owner_id)
    if not account:
        return []
    blacklist = account.setdefault("settings", {}).setdefault("blacklist", [])
    if user_id in blacklist:
        blacklist.remove(user_id)
    data["accounts"][owner_id] = account
    _save(data)
    return blacklist


def is_blacklisted(owner_id, user_id: int) -> bool:
    return user_id in get_blacklist(owner_id)


# ---------------------------------------------------------------------------
# Stats digest (Pro) — periodic summary auto-sent to Saved Messages.
# ---------------------------------------------------------------------------

def get_last_digest_at(owner_id) -> str | None:
    account = _load()["accounts"].get(str(owner_id))
    if not account:
        return None
    return account.get("last_digest_at")


def set_last_digest_at(owner_id, iso_timestamp: str) -> None:
    owner_id = str(owner_id)
    data = _load()
    account = data["accounts"].get(owner_id)
    if not account:
        return
    account["last_digest_at"] = iso_timestamp
    data["accounts"][owner_id] = account
    _save(data)


# Har bir ommaviy funksiya qulf ostida ishlaydi (o'qish-o'zgartirish-yozish poygasiz).
def _lock_public() -> None:
    g = globals()
    for name, fn in list(g.items()):
        if name.startswith("_") or not callable(fn) or getattr(fn, "__module__", None) != __name__:
            continue
        if isinstance(fn, type):
            continue

        def make(f):
            @functools.wraps(f)
            def wrapper(*a, **k):
                with _lock:
                    return f(*a, **k)
            return wrapper
        g[name] = make(fn)


_lock_public()
