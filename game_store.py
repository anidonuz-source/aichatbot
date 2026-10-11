"""
Misumi AI — duel game data store.

Tracks per-user win/loss/draw stats for the dice-emoji duel game
(/duel). Ma'lumot Postgres'da (db.py); Render diski vaqtinchalik, shuning
uchun fayl ishlatilmaydi. Eski _game_data.json bo'lsa o'zi ko'chiriladi.
"""
import functools
import os
from pathlib import Path
from threading import RLock

import db

STORE_DIR = Path(os.environ.get("MEMORY_DIR", "memory"))
STORE_DIR.mkdir(parents=True, exist_ok=True)
LEGACY_FILE = STORE_DIR / "_game_data.json"
_STORE, _KEY = "gamestore", "main"

_lock = RLock()


def _safe(default):
    """Qulf ostida ishlaydi; baza xatosida o'yin to'xtamaydi (default qaytadi)."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            with _lock:
                try:
                    return fn(*a, **k)
                except Exception as e:
                    print(f"[GameStore] {fn.__name__} xato: {e}")
                    return default() if callable(default) else default
        return wrapper
    return deco


def _empty() -> dict:
    return {"users": {}}


def _load() -> dict:
    data = db.load_migrating(_STORE, _KEY, LEGACY_FILE, None)
    base = _empty()
    if isinstance(data, dict):
        base.update(data)
    return base


def _save(data: dict) -> None:
    db.save(_STORE, _KEY, data)


def _entry(data: dict, user_id: str, name: str | None) -> dict:
    users = data.setdefault("users", {})
    e = users.get(user_id) or {"wins": 0, "losses": 0, "draws": 0, "name": name or user_id}
    if name:
        e["name"] = name
    users[user_id] = e
    return e


@_safe(None)
def record_result(
    winner_id: str | None,
    winner_name: str | None,
    loser_id: str | None,
    loser_name: str | None,
    draw: bool = False,
) -> None:
    """Record the outcome of one duel. Pass draw=True with both ids/names
    set (winner_id/loser_id then just mean "player A"/"player B") to log
    a draw for both without affecting win/loss counts."""
    data = _load()
    if draw:
        if winner_id:
            _entry(data, str(winner_id), winner_name)["draws"] += 1
        if loser_id:
            _entry(data, str(loser_id), loser_name)["draws"] += 1
    else:
        if winner_id:
            _entry(data, str(winner_id), winner_name)["wins"] += 1
        if loser_id:
            _entry(data, str(loser_id), loser_name)["losses"] += 1
    _save(data)


@_safe(lambda: {"wins": 0, "losses": 0, "draws": 0})
def get_stats(user_id) -> dict:
    data = _load()
    e = data.get("users", {}).get(str(user_id))
    if not e:
        return {"wins": 0, "losses": 0, "draws": 0}
    return e


@_safe(list)
def get_leaderboard(limit: int = 10) -> list[dict]:
    data = _load()
    users = data.get("users", {})
    result = []
    for uid, info in users.items():
        wins = info.get("wins", 0)
        losses = info.get("losses", 0)
        result.append(
            {
                "id": uid,
                "name": info.get("name") or uid,
                "wins": wins,
                "losses": losses,
                "draws": info.get("draws", 0),
                "total": wins + losses,
            }
        )
    # Rank by wins first, then by win rate among players with games played.
    result.sort(key=lambda u: (u["wins"], u["wins"] - u["losses"]), reverse=True)
    return [u for u in result if u["total"] > 0][:limit]
