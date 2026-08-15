"""
Misumi AI — admin data store.

Tracks lightweight usage stats, blocked/premium users, and a maintenance
flag. Stored as a single JSON file alongside per-user memory files (so it
persists on the same Render disk if MEMORY_DIR points at one).

This is intentionally simple (file + lock) — fine for a personal/small
group bot. Not meant for high-concurrency production use.
"""
import json
import os
from datetime import datetime
from pathlib import Path
from threading import Lock

STORE_DIR = Path(os.environ.get("MEMORY_DIR", "memory"))
STORE_DIR.mkdir(parents=True, exist_ok=True)
STORE_FILE = STORE_DIR / "_admin_data.json"

_lock = Lock()


def _empty() -> dict:
    return {"maintenance": False, "blocked": [], "premium": [], "users": {}, "broadcasts": []}


def _load() -> dict:
    if not STORE_FILE.exists():
        return _empty()
    try:
        data = json.loads(STORE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _empty()
        base = _empty()
        base.update(data)
        return base
    except Exception as e:
        print(f"[AdminStore] Load error: {e}")
        return _empty()


def _save(data: dict) -> None:
    with _lock:
        STORE_FILE.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def record_message(user_id, name: str | None = None, source: str = "telegram") -> None:
    """Call once per incoming user message to keep stats fresh."""
    user_id = str(user_id)
    data = _load()
    users = data.setdefault("users", {})
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = users.get(user_id) or {"first_seen": now, "message_count": 0}
    entry["last_seen"] = now
    entry["message_count"] = entry.get("message_count", 0) + 1
    entry["source"] = source
    if name:
        entry["name"] = name
    users[user_id] = entry
    _save(data)


def is_blocked(user_id) -> bool:
    return str(user_id) in _load().get("blocked", [])


def is_premium(user_id) -> bool:
    return str(user_id) in _load().get("premium", [])


def is_maintenance() -> bool:
    return bool(_load().get("maintenance", False))


def set_maintenance(value: bool) -> bool:
    data = _load()
    data["maintenance"] = bool(value)
    _save(data)
    return data["maintenance"]


def toggle_block(user_id) -> bool:
    """Returns the new blocked state (True = now blocked)."""
    user_id = str(user_id)
    data = _load()
    blocked = set(data.get("blocked", []))
    if user_id in blocked:
        blocked.discard(user_id)
        state = False
    else:
        blocked.add(user_id)
        state = True
    data["blocked"] = sorted(blocked)
    _save(data)
    return state


def toggle_premium(user_id) -> bool:
    """Returns the new premium state (True = now premium)."""
    user_id = str(user_id)
    data = _load()
    premium = set(data.get("premium", []))
    if user_id in premium:
        premium.discard(user_id)
        state = False
    else:
        premium.add(user_id)
        state = True
    data["premium"] = sorted(premium)
    _save(data)
    return state


def get_stats() -> dict:
    data = _load()
    users = data.get("users", {})
    return {
        "total_users": len(users),
        "total_messages": sum(u.get("message_count", 0) for u in users.values()),
        "maintenance": data.get("maintenance", False),
        "blocked_count": len(data.get("blocked", [])),
        "premium_count": len(data.get("premium", [])),
    }


def get_users(limit: int = 300) -> list[dict]:
    data = _load()
    users = data.get("users", {})
    blocked = set(data.get("blocked", []))
    premium = set(data.get("premium", []))
    result = []
    for uid, info in users.items():
        result.append(
            {
                "id": uid,
                "name": info.get("name") or uid,
                "first_seen": info.get("first_seen"),
                "last_seen": info.get("last_seen"),
                "message_count": info.get("message_count", 0),
                "source": info.get("source", "telegram"),
                "blocked": uid in blocked,
                "premium": uid in premium,
            }
        )
    result.sort(key=lambda u: u["last_seen"] or "", reverse=True)
    return result[:limit]


def get_all_chat_ids() -> list[str]:
    """Every chat/user id Misumi has ever exchanged a message with
    (private chats and groups alike, since record_message is called
    with whichever chat_id the message came from) — used for
    /broadcast. Unlike get_users(), never truncated."""
    return list(_load().get("users", {}).keys())


def record_broadcast(summary: str, sent: int, failed: int) -> None:
    """Log a completed /broadcast run for /broadcast_history. Keeps only
    the most recent 20 entries."""
    data = _load()
    entries = data.setdefault("broadcasts", [])
    entries.append(
        {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "summary": summary[:120],
            "sent": sent,
            "failed": failed,
        }
    )
    data["broadcasts"] = entries[-20:]
    _save(data)


def get_broadcast_history(limit: int = 10) -> list[dict]:
    data = _load()
    return list(reversed(data.get("broadcasts", [])))[:limit]
