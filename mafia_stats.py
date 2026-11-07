"""
Misumi AI — Mafia statistikasi, reyting va yutuqlar
===================================================
Tugagan o'yin natijalarini yig'adi: o'yinlar, g'alabalar, rol bo'yicha winrate,
haftalik top-10 va yutuqlar. Ma'lumot db.py (Postgres) orqali saqlanadi; baza
ulanmagan bo'lsa xotirada ishlaydi (qayta ishga tushganda yo'qoladi).
"""
import threading
import time

STORE, KEY = "mafia", "stats"
_LOCK = threading.RLock()
_data: dict | None = None
_dirty = False
_last_save = 0.0

ACHIEVEMENTS = {
    "first_win":   {"emoji": "🏆", "name": "Birinchi g'alaba",   "desc": "Birinchi o'yiningda yut."},
    "veteran":     {"emoji": "🎖", "name": "Faxriy",             "desc": "25 ta o'yin o'yna."},
    "winner10":    {"emoji": "🥇", "name": "O'n g'alaba",        "desc": "10 marta yut."},
    "streak3":     {"emoji": "🔥", "name": "Ketma-ket uchlik",   "desc": "Ketma-ket 3 marta yut."},
    "mafia_boss":  {"emoji": "🎩", "name": "Cho'qintirgan ota",  "desc": "Don bo'lib yut."},
    "sheriff":     {"emoji": "🕵️", "name": "Ishonchli komissar", "desc": "Komissar bo'lib yut."},
    "medic":       {"emoji": "🩺", "name": "Hayot saqlovchi",    "desc": "Doktor bo'lib yut."},
    "lone_wolf":   {"emoji": "🐺", "name": "Yolg'iz bo'ri",      "desc": "Maniak yoki Vampir bo'lib yut."},
    "survivor":    {"emoji": "🛡", "name": "Omon qolgan",        "desc": "G'alaba qilgan jamoada oxirigacha tirik qol."},
    "sharpshooter": {"emoji": "🎯", "name": "Aniq nishon",       "desc": "Snayper bo'lib mafiyani ot va yut."},
    "judge_veto":  {"emoji": "⚖️", "name": "Oxirgi so'z sudyada", "desc": "Sudya bo'lib ovozni bekor qil."},
    "heartbreak":  {"emoji": "💔", "name": "Sevgi qurboni",      "desc": "Sevishgan sherigingiz ketgani uchun o'l."},
}


def _empty() -> dict:
    return {"players": {}, "weekly": {}}


def _load() -> dict:
    global _data
    if _data is not None:
        return _data
    try:
        import db
        _data = db.load(STORE, KEY, None) or _empty()
    except Exception as e:  # baza yo'q: xotirada ishlaymiz
        print(f"[mafia_stats] baza o'qilmadi, xotirada ishlayman: {e}")
        _data = _empty()
    _data.setdefault("players", {})
    _data.setdefault("weekly", {})
    return _data


def _save(force: bool = False) -> None:
    global _dirty, _last_save
    now = time.time()
    if not _dirty or (not force and now - _last_save < 5):
        return
    try:
        import db
        db.save(STORE, KEY, _data)
        _dirty = False
        _last_save = now
    except Exception as e:
        print(f"[mafia_stats] saqlab bo'lmadi: {e}")


def week_key(ts: float | None = None) -> str:
    t = time.gmtime(ts or time.time())
    iso = time.strftime("%G-W%V", t)
    return iso


def _player(uid: int, name: str) -> dict:
    d = _load()["players"].setdefault(str(uid), {
        "name": name, "games": 0, "wins": 0, "streak": 0, "best_streak": 0,
        "by_role": {}, "ach": [], "kills": 0, "survived": 0,
    })
    d["name"] = name
    return d


def _grant(d: dict, key: str, new: list) -> None:
    if key not in d["ach"]:
        d["ach"].append(key)
        new.append(key)


def record_game(game) -> dict[int, list[str]]:
    """Tugagan o'yinni yozadi. Qaytaradi: uid -> yangi olingan yutuqlar."""
    if game.cancelled or not game.winner:
        return {}
    earned: dict[int, list[str]] = {}
    wk = week_key()
    with _LOCK:
        data = _load()
        wk_bucket = data["weekly"].setdefault(wk, {})
        for old in sorted(data["weekly"])[:-6]:       # faqat oxirgi 6 hafta
            del data["weekly"][old]
        for p in game.players.values():
            if p.is_bot or p.uid <= 0 or not p.role:
                continue
            won = p.team == game.winner
            d = _player(p.uid, p.name)
            d["games"] += 1
            r = d["by_role"].setdefault(p.role, {"g": 0, "w": 0})
            r["g"] += 1
            if won:
                d["wins"] += 1
                r["w"] += 1
                d["streak"] += 1
                d["best_streak"] = max(d["best_streak"], d["streak"])
            else:
                d["streak"] = 0
            if p.alive and won:
                d["survived"] += 1
            w = wk_bucket.setdefault(str(p.uid), {"name": p.name, "games": 0, "wins": 0})
            w["name"] = p.name
            w["games"] += 1
            w["wins"] += 1 if won else 0

            new: list[str] = []
            if won and d["wins"] == 1:
                _grant(d, "first_win", new)
            if d["games"] >= 25:
                _grant(d, "veteran", new)
            if d["wins"] >= 10:
                _grant(d, "winner10", new)
            if d["streak"] >= 3:
                _grant(d, "streak3", new)
            if won and p.role == "don":
                _grant(d, "mafia_boss", new)
            if won and p.role == "commissar":
                _grant(d, "sheriff", new)
            if won and p.role == "doctor":
                _grant(d, "medic", new)
            if won and p.role in ("maniac", "vampire"):
                _grant(d, "lone_wolf", new)
            if won and p.alive:
                _grant(d, "survivor", new)
            if won and p.role == "sniper" and getattr(p, "sniper_hit_mafia", False):
                _grant(d, "sharpshooter", new)
            if getattr(p, "vetoed", False):
                _grant(d, "judge_veto", new)
            if p.death and p.death.get("cause") == "heartbreak":
                _grant(d, "heartbreak", new)
            if new:
                earned[p.uid] = new
        global _dirty
        _dirty = True
        _save(force=True)
    return earned


def _winrate(g: int, w: int) -> int:
    return round(100 * w / g) if g else 0


def leaderboard(period: str = "week", limit: int = 10) -> list[dict]:
    """period: 'week' | 'all'. Tartib: g'alabalar, keyin winrate, keyin o'yinlar."""
    with _LOCK:
        data = _load()
        if period == "week":
            src = data["weekly"].get(week_key(), {})
            rows = [{"uid": int(u), "name": v["name"], "games": v["games"], "wins": v["wins"]} for u, v in src.items()]
        else:
            rows = [{"uid": int(u), "name": v["name"], "games": v["games"], "wins": v["wins"]}
                    for u, v in data["players"].items()]
    for r in rows:
        r["winrate"] = _winrate(r["games"], r["wins"])
    rows = [r for r in rows if r["games"] > 0]
    rows.sort(key=lambda r: (-r["wins"], -r["winrate"], -r["games"]))
    return rows[:limit]


def profile(uid: int, name: str = "") -> dict:
    with _LOCK:
        d = _load()["players"].get(str(uid))
        if not d:
            return {"uid": uid, "name": name, "games": 0, "wins": 0, "winrate": 0, "best_streak": 0,
                    "roles": [], "achievements": [], "rank": None}
        roles = sorted(
            ({"role": k, "games": v["g"], "wins": v["w"], "winrate": _winrate(v["g"], v["w"])}
             for k, v in d["by_role"].items()),
            key=lambda x: -x["games"])
        ach = [dict(ACHIEVEMENTS[k], key=k) for k in d["ach"] if k in ACHIEVEMENTS]
        allrows = sorted(_load()["players"].items(), key=lambda kv: (-kv[1]["wins"], -kv[1]["games"]))
        rank = next((i + 1 for i, (u, _) in enumerate(allrows) if u == str(uid)), None)
        return {"uid": uid, "name": d["name"], "games": d["games"], "wins": d["wins"],
                "winrate": _winrate(d["games"], d["wins"]), "best_streak": d["best_streak"],
                "survived": d.get("survived", 0), "roles": roles, "achievements": ach, "rank": rank}


def flush() -> None:
    with _LOCK:
        _save(force=True)
