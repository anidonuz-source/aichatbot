"""
Misumi AI — sticker library.

Stores Telegram sticker file_ids grouped by category (mood for normal
chat, win/lose/draw for the duel game). Same JSON+lock pattern as
admin_store.py / game_store.py.

There's no way to pre-fill real file_ids from outside Telegram, so the
library starts empty. Populate it live in chat: reply to any sticker
with `/stiker <category>` (e.g. `/stiker win`, `/stiker happy`) and
Misumi AI saves that sticker's file_id under that category. From then
on she'll pick a random one from that category whenever it fits.

See CATEGORIES below for the full list of valid category names.
"""
import json
import os
import random
from pathlib import Path
from threading import Lock

STORE_DIR = Path(os.environ.get("MEMORY_DIR", "memory"))
STORE_DIR.mkdir(parents=True, exist_ok=True)
STORE_FILE = STORE_DIR / "_sticker_data.json"

_lock = Lock()

# Mood categories used in normal chat + result categories used by the
# duel game. Kept in one list so /stiker validates against both uses.
CHAT_CATEGORIES = ["happy", "laugh", "love", "sad", "angry", "surprised", "cool", "thinking"]
GAME_CATEGORIES = ["win", "lose", "draw"]
CATEGORIES = CHAT_CATEGORIES + GAME_CATEGORIES


def _empty() -> dict:
    return {"categories": {c: [] for c in CATEGORIES}}


def _load() -> dict:
    if not STORE_FILE.exists():
        return _empty()
    try:
        data = json.loads(STORE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _empty()
        base = _empty()
        cats = base["categories"]
        cats.update(data.get("categories", {}))
        return {"categories": cats}
    except Exception as e:
        print(f"[StickerStore] Load error: {e}")
        return _empty()


def _save(data: dict) -> None:
    with _lock:
        STORE_FILE.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def add_sticker(category: str, file_id: str) -> bool:
    """Add a sticker's file_id under a category. Returns False if the
    category name isn't recognized."""
    category = category.strip().lower()
    if category not in CATEGORIES:
        return False
    data = _load()
    ids = data["categories"].setdefault(category, [])
    if file_id not in ids:
        ids.append(file_id)
    _save(data)
    return True


def get_random(category: str) -> str | None:
    """Return a random sticker file_id from a category, or None if that
    category is empty (or unrecognized) — caller should just skip
    sending a sticker in that case, never error."""
    data = _load()
    ids = data["categories"].get(category) or []
    return random.choice(ids) if ids else None


def counts() -> dict[str, int]:
    data = _load()
    return {c: len(ids) for c, ids in data["categories"].items()}
