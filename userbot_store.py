"""
Misumi AI — userbot data store.

Stores, per bot-user (the person who connected their personal Telegram
account), an ENCRYPTED Telethon session string plus their feature
settings (auto-reply when offline, auto-bio) and lightweight stats.

Session strings grant full access to a Telegram account, so they are
never stored in plaintext. Encryption uses Fernet (AES128-CBC + HMAC)
with a key read from the USERBOT_ENC_KEY env var. If that var is not
set, a key is generated and persisted next to the store file so local
testing still works — but on a real deployment you MUST set
USERBOT_ENC_KEY yourself (see .env.example) or sessions won't survive
a redeploy without it.
"""
import json
import os
from datetime import datetime
from pathlib import Path
from threading import Lock

from cryptography.fernet import Fernet

STORE_DIR = Path(os.environ.get("MEMORY_DIR", "memory"))
STORE_DIR.mkdir(parents=True, exist_ok=True)
STORE_FILE = STORE_DIR / "_userbot_data.json"
KEY_FILE = STORE_DIR / "_userbot.key"

_lock = Lock()


def _load_key() -> bytes:
    env_key = os.environ.get("USERBOT_ENC_KEY", "").strip()
    if env_key:
        return env_key.encode("utf-8")
    if KEY_FILE.exists():
        return KEY_FILE.read_text(encoding="utf-8").strip().encode("utf-8")
    key = Fernet.generate_key()
    KEY_FILE.write_text(key.decode("utf-8"), encoding="utf-8")
    print(
        "[UserbotStore] WARNING: USERBOT_ENC_KEY not set — generated a local "
        f"key at {KEY_FILE}. Set USERBOT_ENC_KEY in production so sessions "
        "survive redeploys."
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
    if not STORE_FILE.exists():
        return _empty()
    try:
        data = json.loads(STORE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "accounts" not in data:
            return _empty()
        return data
    except Exception as e:
        print(f"[UserbotStore] Load error: {e}")
        return _empty()


def _save(data: dict) -> None:
    with _lock:
        STORE_FILE.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def _default_account(phone: str) -> dict:
    return {
        "phone": phone,
        "session": None,
        "connected_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "settings": {"auto_reply": False, "auto_bio": False, "inner_ai": False, "offline_minutes": None},
        "stats": {"auto_replies_sent": 0, "bio_updates": 0, "self_chat_replies": 0},
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
