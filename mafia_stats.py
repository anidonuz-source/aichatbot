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
    "veteran50":   {"emoji": "🏅", "name": "Yarim yuzlik",       "desc": "50 ta o'yin o'yna."},
    "streak5":     {"emoji": "⚡", "name": "Cheksiz seriya",     "desc": "Ketma-ket 5 marta yut."},
    "rich":        {"emoji": "💰", "name": "Boy",                "desc": "Hisobingda 1000 💵 to'pla."},
    "shopper":     {"emoji": "🛍", "name": "Xaridor",            "desc": "Do'kondan birinchi mahsulotni sotib ol."},
    "heartbreak":  {"emoji": "💔", "name": "Sevgi qurboni",      "desc": "Sevishgan sherigingiz ketgani uchun o'l."},
}


CURRENCY = "💵"
START_COINS, DAILY_COINS, MAX_ITEM = 100, 50, 3
REWARD_PLAY, REWARD_WIN, REWARD_ALIVE = 20, 50, 15
# Do'kon: yangi mahsulot qo'shish uchun shu yerga yoz (mafia.py _apply_items da ishlatiladi)
SHOP = {
    "shield":         {"emoji": "🛡", "name": "Tun himoyasi",   "price": 150, "desc": "Keyingi o'yinda tunda bitta hujumdan omon qolasan."},
    "vote_shield":    {"emoji": "🗳", "name": "Ovozdan himoya", "price": 120, "desc": "Keyingi o'yinda osishga ovoz yig'ilsa, bir marta osilmaysan."},
    "coin_boost":     {"emoji": "🚀", "name": "Mukofot ×2",     "price": 200, "desc": "Keyingi o'yindagi pul mukofoti ikki baravar."},
    "role_sniper":    {"emoji": "🔫", "name": "Pistolet (Snayper roli)", "price": 300,
                       "desc": "Keyingi o'yinda Snayper bo'lasan (1 ta o'q)."},
    "role_commissar": {"emoji": "🕵️", "name": "Komissar roli", "price": 350,
                       "desc": "Keyingi o'yinda Komissar bo'lasan, har tun bitta odamni tekshirasan."},
    "role_doctor":    {"emoji": "🩺", "name": "Doktor roli",    "price": 300,
                       "desc": "Keyingi o'yinda Doktor bo'lasan, har tun bitta odamni davolaysan."},
}
# Rol sotib olish faqat shahar jamoasida chiqsang ishlaydi; aks holda mahsulot saqlanib qoladi.
TITLES = ["Yangi kelgan", "Fuqaro", "Kuzatuvchi", "Tergovchi", "Mafiya a'zosi", "Shahar himoyachisi", "Don", "Afsonaviy"]


def level_info(xp: int) -> dict:
    lvl = int((max(0, xp) / 50) ** 0.5) + 1
    return {"level": lvl, "title": TITLES[min(len(TITLES) - 1, (lvl - 1) // 2)], "xp": xp,
            "lo": 50 * (lvl - 1) ** 2, "hi": 50 * lvl ** 2}


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
    d.setdefault("coins", START_COINS)
    d.setdefault("inv", {})
    d.setdefault("daily_at", 0)
    d.setdefault("xp", 0)
    d.setdefault("earned", 0)
    d.setdefault("hist", [])
    d.setdefault("since", time.time())
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
    gains: dict[int, int] = {}
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
            gain = REWARD_PLAY + (REWARD_WIN if won else 0) + (REWARD_ALIVE if (won and p.alive) else 0)
            if getattr(p, "boost", False):
                gain *= 2
            d["coins"] += gain
            d["earned"] += gain
            d["xp"] += 10 + (25 if won else 0) + (10 if (won and p.alive) else 0)
            gains[p.uid] = gain
            d["hist"] = ([{"t": time.time(), "role": p.role, "won": won, "gain": gain,
                           "grp": (getattr(game, "chat_title", "") or "")[:30]}] + d["hist"])[:10]
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
            if d["games"] >= 50:
                _grant(d, "veteran50", new)
            if d["streak"] >= 5:
                _grant(d, "streak5", new)
            if d["coins"] >= 1000:
                _grant(d, "rich", new)
            if p.death and p.death.get("cause") == "heartbreak":
                _grant(d, "heartbreak", new)
            if new:
                earned[p.uid] = new
        cid = getattr(game, "chat_id", 0) or 0
        if cid < 0:
            gr = data.setdefault("groups", {}).setdefault(str(cid), {"title": "", "games": 0, "uids": [], "last": 0})
            gr["title"] = (getattr(game, "chat_title", "") or gr["title"] or "Guruh")[:40]
            gr["games"] += 1
            gr.setdefault("wins", {})
            gr["wins"][game.winner] = gr["wins"].get(game.winner, 0) + 1
            gr["last"] = time.time()
            for uid in gains:
                if uid not in gr["uids"] and len(gr["uids"]) < 500:
                    gr["uids"].append(uid)
        game.coins_earned = gains
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
                    "roles": [], "achievements": [], "rank": None, "coins": START_COINS, "inv": {}, "xp": 0, "earned": 0, "streak": 0,
                    "hist": [], "since": None, "lvl": level_info(0)}
        roles = sorted(
            ({"role": k, "games": v["g"], "wins": v["w"], "winrate": _winrate(v["g"], v["w"])}
             for k, v in d["by_role"].items()),
            key=lambda x: -x["games"])
        ach = [dict(ACHIEVEMENTS[k], key=k) for k in d["ach"] if k in ACHIEVEMENTS]
        allrows = sorted(_load()["players"].items(), key=lambda kv: (-kv[1]["wins"], -kv[1]["games"]))
        rank = next((i + 1 for i, (u, _) in enumerate(allrows) if u == str(uid)), None)
        return {"uid": uid, "name": d["name"], "games": d["games"], "wins": d["wins"],
                "winrate": _winrate(d["games"], d["wins"]), "best_streak": d["best_streak"],
                "survived": d.get("survived", 0), "coins": d.get("coins", START_COINS), "inv": d.get("inv", {}), "xp": d.get("xp", 0), "earned": d.get("earned", 0), "streak": d.get("streak", 0),
                "hist": d.get("hist", []), "since": d.get("since"), "lvl": level_info(d.get("xp", 0)), "roles": roles, "achievements": ach, "rank": rank}


def top_groups(limit: int = 10) -> list[dict]:
    with _LOCK:
        gs = _load().get("groups", {})
        rows = [{"title": g["title"] or "Guruh", "games": g["games"], "players": len(g.get("uids", [])), "wins": g.get("wins", {})} for g in gs.values()]
    rows.sort(key=lambda r: (-r["games"], -r["players"]))
    return rows[:limit]


def buy(uid: int, name: str, item: str) -> tuple[bool, str]:
    global _dirty
    it = SHOP.get(item)
    if not it:
        return False, "Bunday mahsulot yo'q"
    with _LOCK:
        d = _player(uid, name)
        if d["inv"].get(item, 0) >= MAX_ITEM:
            return False, f"Bitta mahsulotdan {MAX_ITEM} tadan ko'p saqlab bo'lmaydi"
        if d["coins"] < it["price"]:
            return False, f"Pul yetmaydi: {it['price']} {CURRENCY} kerak"
        d["coins"] -= it["price"]
        d["inv"][item] = d["inv"].get(item, 0) + 1
        _grant(d, "shopper", [])
        _dirty = True
        _save(force=True)
    return True, f"{it['emoji']} {it['name']} sotib olindi"


def use(uid: int, item: str) -> bool:
    """O'yin boshlanganda mahsulotni sarflaydi. Bo'lmasa False."""
    global _dirty
    with _LOCK:
        d = _load()["players"].get(str(uid))
        if not d or d.get("inv", {}).get(item, 0) <= 0:
            return False
        d["inv"][item] -= 1
        _dirty = True
        _save()
        return True


def inventory(uid: int) -> dict:
    with _LOCK:
        d = _load()["players"].get(str(uid))
        return dict(d.get("inv", {})) if d else {}


def daily_info(uid: int) -> tuple[bool, int]:
    with _LOCK:
        d = _load()["players"].get(str(uid))
        left = 0 if not d else max(0, int(d.get("daily_at", 0) + 86400 - time.time()))
    return left == 0, left


def claim_daily(uid: int, name: str) -> tuple[bool, str]:
    global _dirty
    with _LOCK:
        d = _player(uid, name)
        left = d["daily_at"] + 86400 - time.time()
        if left > 0:
            return False, f"Kunlik sovg'a {int(left // 3600) + 1} soatdan keyin"
        d["daily_at"] = time.time()
        d["coins"] += DAILY_COINS
        _dirty = True
        _save(force=True)
    return True, f"🎁 +{DAILY_COINS} {CURRENCY}"


def richest(limit: int = 10) -> list[dict]:
    with _LOCK:
        rows = [{"uid": int(u), "name": v["name"], "coins": v.get("coins", START_COINS)} for u, v in _load()["players"].items()]
    rows.sort(key=lambda r: -r["coins"])
    return rows[:limit]


def rank_of_coins(uid: int):
    rows = richest(100000)
    return next((i + 1 for i, r in enumerate(rows) if r["uid"] == uid), None)


def groups_of(uid: int) -> list[dict]:
    with _LOCK:
        gs = [{"title": g["title"] or "Guruh", "games": g["games"]} for g in _load().get("groups", {}).values() if uid in g.get("uids", [])]
    gs.sort(key=lambda g: -g["games"])
    return gs


def flush() -> None:
    with _LOCK:
        _save(force=True)
