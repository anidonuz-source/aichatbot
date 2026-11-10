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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

STORE_DIR = Path(os.environ.get("MEMORY_DIR", "memory"))
STORE_DIR.mkdir(parents=True, exist_ok=True)
STORE_FILE = STORE_DIR / "_admin_data.json"

_lock = Lock()
_TZ = timezone(timedelta(hours=5))  # Toshkent


def _today() -> str:
    return datetime.now(_TZ).strftime("%Y-%m-%d")


def _empty() -> dict:
    return {"maintenance": False, "blocked": [], "premium": [], "users": {}, "broadcasts": []}


_STORE = "admin"
_KEY = "data"


def _load() -> dict:
    """Postgres'dan o'qiydi (Render diski har restartda o'chadi).
    Baza xatosida bo'sh dict QAYTARMAYDI — aks holda keyingi _save() hamma
    statistikani o'chirib yuborardi. Xato ko'tariladi."""
    import db
    data = db.load_migrating(_STORE, _KEY, STORE_FILE, None)
    if not isinstance(data, dict):
        return _empty()
    base = _empty()
    base.update(data)
    return base


def _save(data: dict) -> None:
    import db
    with _lock:
        db.save(_STORE, _KEY, data)


def record_message(user_id, name: str | None = None, source: str = "telegram") -> None:
    """Call once per incoming user message to keep stats fresh."""
    user_id = str(user_id)
    try:
        data = _load()
    except Exception as e:
        print(f"[AdminStore] record_message: baza o'qilmadi, o'tkazib yuborildi: {e}")
        return
    users = data.setdefault("users", {})
    now = datetime.now(_TZ).strftime("%Y-%m-%d %H:%M")
    entry = users.get(user_id) or {"first_seen": now, "message_count": 0}
    entry["last_seen"] = now
    entry["message_count"] = entry.get("message_count", 0) + 1
    entry["source"] = source
    if name:
        entry["name"] = name
    users[user_id] = entry
    try:
        if int(user_id) > 0:  # guruhlar kunlik foydalanuvchi sonini buzmasin
            daily = data.setdefault("daily", {})
            day = daily.setdefault(_today(), {"m": 0, "u": []})
            day["m"] += 1
            if user_id not in day["u"]:
                day["u"].append(user_id)
            for k in sorted(daily)[:-14]:  # faqat oxirgi 14 kun
                daily.pop(k, None)
    except ValueError:
        pass
    try:
        _save(data)
    except Exception as e:
        print(f"[AdminStore] record_message: saqlanmadi: {e}")


def is_blocked(user_id) -> bool:
    try:
        return str(user_id) in _load().get("blocked", [])
    except Exception:
        return False


def _expired(data: dict, uid: str) -> bool:
    """premium_until (epoch soniya) o'tib ketgan bo'lsa True. Yozuv yo'q = cheksiz."""
    until = (data.get("premium_until") or {}).get(uid)
    return bool(until) and float(until) <= datetime.now(timezone.utc).timestamp()


def is_premium(user_id) -> bool:
    try:
        data = _load()
        uid = str(user_id)
        return uid in data.get("premium", []) and not _expired(data, uid)
    except Exception:
        return False


def premium_until(user_id):
    """Tugash vaqti (epoch soniya) yoki None (cheksiz / premium emas)."""
    try:
        data = _load()
        uid = str(user_id)
        if uid not in data.get("premium", []) or _expired(data, uid):
            return None
        return (data.get("premium_until") or {}).get(uid)
    except Exception:
        return None


def grant_premium(user_id, days: int | None = None):
    """Premium beradi. days=None/0 -> cheksiz. Allaqachon faol bo'lsa muddat ustiga qo'shiladi.
    Qaytaradi: tugash vaqti (epoch soniya) yoki None (cheksiz)."""
    uid = str(user_id)
    data = _load()
    premium = set(data.get("premium", []))
    until_map = data.setdefault("premium_until", {})
    now = datetime.now(timezone.utc).timestamp()
    if days:
        cur = until_map.get(uid)
        base = float(cur) if (uid in premium and cur and float(cur) > now) else now
        until = int(base + days * 86400)
        until_map[uid] = until
    else:
        until = None
        until_map.pop(uid, None)
    premium.add(uid)
    data["premium"] = sorted(premium)
    _save(data)
    return until


def revoke_premium(user_id) -> None:
    uid = str(user_id)
    data = _load()
    data["premium"] = sorted(set(data.get("premium", [])) - {uid})
    (data.get("premium_until") or {}).pop(uid, None)
    _save(data)


def is_maintenance() -> bool:
    try:
        return bool(_load().get("maintenance", False))
    except Exception:
        return False


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
        (data.get("premium_until") or {}).pop(user_id, None)
        state = False
    else:
        premium.add(user_id)
        state = True
    data["premium"] = sorted(premium)
    _save(data)
    return state


def get_activity(data: dict, days: int = 7) -> list[dict]:
    """Oxirgi `days` kun: [{date, messages, users}] (Toshkent kunlari, eskidan yangiga)."""
    daily = data.get("daily", {})
    out = []
    for n in range(days - 1, -1, -1):
        d = (datetime.now(_TZ) - timedelta(days=n)).strftime("%Y-%m-%d")
        e = daily.get(d, {})
        out.append({"date": d, "messages": e.get("m", 0), "users": len(e.get("u", []))})
    return out


def get_stats() -> dict:
    data = _load()
    people = {k: v for k, v in data.get("users", {}).items() if not k.startswith("-")}
    groups = len(data.get("users", {})) - len(people)
    activity = get_activity(data, 14)
    return {
        "total_users": len(people),
        "total_groups": groups,
        "total_messages": sum(u.get("message_count", 0) for u in people.values()),
        "today_messages": activity[-1]["messages"],
        "today_users": activity[-1]["users"],
        "activity": activity,
        "maintenance": data.get("maintenance", False),
        "blocked_count": len(data.get("blocked", [])),
        "premium_count": sum(1 for u in data.get("premium", []) if not _expired(data, u)),
    }


def get_users(limit: int = 3000) -> list[dict]:
    data = _load()
    users = data.get("users", {})
    blocked = set(data.get("blocked", []))
    premium = {u for u in data.get("premium", []) if not _expired(data, u)}
    until_map = data.get("premium_until") or {}
    result = []
    for uid, info in users.items():
        if uid.startswith("-"):
            continue  # guruhlar alohida tabda
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
                "premium_until": until_map.get(uid) if uid in premium else None,
            }
        )
    result.sort(key=lambda u: u["last_seen"] or "", reverse=True)
    return result[:limit]


def get_group_last_seen() -> dict[str, str]:
    """{chat_id: last_seen} for every GROUP chat Misumi has talked in.

    handle_message() in bot.py calls get_ai_reply(chat_id, ...) for
    group messages, which in turn calls record_message(chat_id, ...) —
    so group chats end up tracked in the same 'users' store as regular
    users. Telegram group/supergroup ids are always negative, while
    user ids are always positive, so that's used here to filter out
    private chats without needing a separate store. Used by the idle
    conversation-starter job to find groups that have gone quiet.
    """
    data = _load()
    out = {}
    for uid, info in data.get("users", {}).items():
        try:
            if int(uid) < 0 and info.get("last_seen"):
                out[uid] = info["last_seen"]
        except ValueError:
            continue
    return out


def record_group_membership(chat_id, title: str | None, is_member: bool) -> None:
    """Track whether the bot is currently a member of a group.
    Called from the my_chat_member handler in bot.py whenever the bot
    is added to or removed/kicked from a group.
    """
    chat_id = str(chat_id)
    data = _load()
    groups = data.setdefault("group_memberships", {})
    now = datetime.now(_TZ).strftime("%Y-%m-%d %H:%M")
    entry = groups.get(chat_id) or {"first_seen": now}
    entry["is_member"] = is_member
    entry["updated"] = now
    if title:
        entry["title"] = title
    if is_member and "joined" not in entry:
        entry["joined"] = now
    groups[chat_id] = entry
    _save(data)


def get_groups(limit: int = 200) -> list[dict]:
    """Return a list of group chats (negative chat_ids) with metadata.
    Used by the admin panel's Groups tab.
    Merges message activity data with explicit membership tracking.
    """
    data = _load()
    users = data.get("users", {})
    blocked = set(data.get("blocked", []))
    memberships = data.get("group_memberships", {})

    # Start from message-activity records (negative IDs = groups)
    result_map: dict[str, dict] = {}
    for uid, info in users.items():
        try:
            if int(uid) >= 0:
                continue
        except ValueError:
            continue
        result_map[uid] = {
            "id": uid,
            "title": info.get("name") or f"Guruh {uid}",
            "last_seen": info.get("last_seen"),
            "message_count": info.get("message_count", 0),
            "blocked": uid in blocked,
            "is_member": None,  # unknown until bot explicitly tracks
        }

    # Overlay membership data
    for gid, minfo in memberships.items():
        if gid not in result_map:
            result_map[gid] = {
                "id": gid,
                "title": minfo.get("title") or f"Guruh {gid}",
                "last_seen": minfo.get("updated"),
                "message_count": 0,
                "blocked": gid in blocked,
                "is_member": minfo.get("is_member"),
            }
        else:
            result_map[gid]["is_member"] = minfo.get("is_member")
            if minfo.get("title"):
                result_map[gid]["title"] = minfo["title"]

    result = sorted(result_map.values(), key=lambda g: g["last_seen"] or "", reverse=True)
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
            "time": datetime.now(_TZ).strftime("%Y-%m-%d %H:%M"),
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


# ---------------------------------------------------------------------------
# Persona — global bot character setting ("yaxshi" | "hard")
# "yaxshi" = friendly, helpful, warm Uzbek AI
# "hard"   = current street-style aggressive Misumi personality (default)
# ---------------------------------------------------------------------------

VALID_PERSONAS = {"yaxshi", "hard"}
DEFAULT_PERSONA = "hard"


def get_persona() -> str:
    """Return the current global persona key. Defaults to 'hard'."""
    return _load().get("persona", DEFAULT_PERSONA)


def set_persona(persona: str) -> str:
    """Set the global persona. Ignores unknown values, returns active persona."""
    persona = persona.lower().strip()
    if persona not in VALID_PERSONAS:
        return get_persona()
    data = _load()
    data["persona"] = persona
    _save(data)
    return persona
