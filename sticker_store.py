"""
Misumi AI — sticker + GIF library.

Stores Telegram sticker file_ids grouped by category (mood for normal
chat, win/lose/draw for the duel game) plus a flat GIF pool. Same
pattern as admin_store.py / game_store.py; ma'lumot Postgres'da (Render diski vaqtinchalik).

Two ways stickers/GIFs get in:
  1. Automatic — any sticker or GIF/animation any group member sends
     gets picked up silently in the background (see game.py's
     collect_sticker/collect_gif handlers). Stickers are sorted by
     category using their own built-in Telegram emoji (EMOJI_TO_CATEGORY
     below); anything that doesn't map to a known mood lands in
     "general" rather than being dropped. GIFs have no emoji, so they
     all go into one flat pool.
  2. Manual — reply to any sticker with `/stiker <category>` to force
     it into a specific category (useful for win/lose/draw, which have
     no natural emoji of their own).

See CATEGORIES below for the full list of valid category names.
"""
import functools
import os
import random
from pathlib import Path
from threading import RLock

import db

STORE_DIR = Path(os.environ.get("MEMORY_DIR", "memory"))
STORE_DIR.mkdir(parents=True, exist_ok=True)
LEGACY_FILE = STORE_DIR / "_sticker_data.json"
_STORE, _KEY = "stickerstore", "main"

_lock = RLock()


def _safe(default):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            with _lock:
                try:
                    return fn(*a, **k)
                except Exception as e:
                    print(f"[StickerStore] {fn.__name__} xato: {e}")
                    return default() if callable(default) else default
        return wrapper
    return deco

# Mood categories used in normal chat + result categories used by the
# duel game. Kept in one list so /stiker validates against both uses.
# "general" is the catch-all for stickers whose emoji doesn't map to a
# specific mood — still collected, still usable, just not mood-targeted.
CHAT_CATEGORIES = ["happy", "laugh", "love", "sad", "angry", "surprised", "cool", "thinking", "general"]
GAME_CATEGORIES = ["win", "lose", "draw"]
CATEGORIES = CHAT_CATEGORIES + GAME_CATEGORIES

# A Telegram sticker carries its own "associated emoji" set by whoever
# made the sticker pack — this is how auto-collection sorts stickers by
# mood without needing to look at the image itself. Not exhaustive by
# design; anything not listed here falls back to "general" (see
# category_for_emoji) so nothing collected is ever wasted.
EMOJI_TO_CATEGORY = {
    "😂": "laugh", "🤣": "laugh", "😆": "laugh", "😹": "laugh",
    "😊": "happy", "😄": "happy", "😃": "happy", "🙂": "happy", "😁": "happy", "🎉": "happy", "🥳": "happy",
    "❤️": "love", "😍": "love", "🥰": "love", "😘": "love", "💕": "love", "💖": "love",
    "😢": "sad", "😭": "sad", "😔": "sad", "☹️": "sad", "😞": "sad", "💔": "sad",
    "😡": "angry", "😠": "angry", "🤬": "angry", "😤": "angry",
    "😮": "surprised", "😲": "surprised", "😱": "surprised", "🤯": "surprised",
    "😎": "cool", "😏": "cool", "🆒": "cool",
    "🤔": "thinking", "🧐": "thinking", "🤨": "thinking",
    "🏆": "win", "🥇": "win", "👑": "win", "💪": "win",
    "😩": "lose", "🤦": "lose",
    "🤝": "draw",
}


def category_for_emoji(emoji: str | None) -> str:
    """Map a sticker's own emoji to a mood category, falling back to
    'general' for anything unrecognized (never returns an invalid
    category, so the result is always safe to pass to add_sticker)."""
    return EMOJI_TO_CATEGORY.get(emoji or "", "general")


def _empty() -> dict:
    return {"categories": {c: [] for c in CATEGORIES}, "gifs": []}


def _load() -> dict:
    data = db.load_migrating(_STORE, _KEY, LEGACY_FILE, None)
    base = _empty()
    if isinstance(data, dict):
        cats = base["categories"]
        cats.update(data.get("categories", {}))
        gifs = data.get("gifs", [])
        if not isinstance(gifs, list):
            gifs = []
        return {"categories": cats, "gifs": gifs}
    return base


def _save(data: dict) -> None:
    db.save(_STORE, _KEY, data)


@_safe(False)
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


@_safe(None)
def get_random(category: str) -> str | None:
    """Return a random sticker file_id from a category, or None if that
    category is empty (or unrecognized) — caller should just skip
    sending a sticker in that case, never error."""
    data = _load()
    ids = data["categories"].get(category) or []
    return random.choice(ids) if ids else None


@_safe(None)
def add_gif(file_id: str) -> None:
    """Add a GIF's file_id to the flat pool (GIFs have no emoji of their
    own, so there's no per-mood sorting — just one shared pool)."""
    data = _load()
    gifs = data["gifs"]
    if file_id not in gifs:
        gifs.append(file_id)
    _save(data)


@_safe(None)
def get_random_gif() -> str | None:
    data = _load()
    gifs = data["gifs"]
    return random.choice(gifs) if gifs else None


@_safe(dict)
def counts() -> dict[str, int]:
    data = _load()
    result = {c: len(ids) for c, ids in data["categories"].items()}
    result["gif"] = len(data["gifs"])
    return result
