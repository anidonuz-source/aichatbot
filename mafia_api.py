"""
Misumi AI — Mafia Mini App API (/mafia, /api/mafia/*)

Har bir so'rov Telegram initData bilan tasdiqlanadi (webapp.verify_init_data),
shuning uchun o'yinchi kimligi soxtalashtirib bo'lmaydi. Barcha o'zgartiruvchi
endpointlar yangilangan ko'rinishni (view) qaytaradi.
"""
import logging
import os

from flask import Blueprint, jsonify, render_template, request

import ai_core
import mafia

bp = Blueprint("mafia_api", __name__)
log = logging.getLogger("mafia_api")


def _user(body: dict):
    import webapp  # kech import: webapp bu blueprintni o'zi ro'yxatdan o'tkazadi
    data = webapp.verify_init_data(body.get("initData", ""))
    if not data or not data.get("user"):
        return None
    return data["user"]


def _photo(user: dict):
    u = str(user.get("photo_url") or "")
    return u if u.startswith("https://t.me/") else None


def _ctx():
    """(user, game, error_response)"""
    body = request.get_json(silent=True) or {}
    user = _user(body)
    if not user:
        return None, None, body, (jsonify({"error": "auth"}), 401)
    gid = str(body.get("gid", "")).strip()
    game = mafia.get_game(gid)
    if not game:
        return user, None, body, (jsonify({"error": "game_not_found"}), 404)
    _p = game.players.get(int(user["id"]))
    if _p is not None and not getattr(_p, "photo", None):
        _p.photo = _photo(user)
    return user, game, body, None


def _view(game: "mafia.Game", uid: int, extra: dict | None = None):
    v = game.view_for(uid)
    if extra:
        v.update(extra)
    return jsonify(v)


@bp.route("/mafia")
def page():
    return render_template("mafia.html", bot_name=ai_core.BOT_NAME,
                           bot_username=(os.environ.get("BOT_USERNAME") or getattr(ai_core, "BOT_USERNAME", "") or "").lstrip("@"))


@bp.route("/api/mafia/state", methods=["POST"])
def state():
    user, game, body, err = _ctx()
    if err:
        return err
    mafia.tick_all()  # bot job kechiksa ham fazalar siljisin
    return _view(game, int(user["id"]))


@bp.route("/api/mafia/join", methods=["POST"])
def join():
    user, game, body, err = _ctx()
    if err:
        return err
    ok, msg = game.add_player(int(user["id"]), user.get("first_name") or "O'yinchi", user.get("username"), False, _photo(user))
    return _view(game, int(user["id"]), {"result": {"ok": ok, "msg": msg}})


@bp.route("/api/mafia/leave", methods=["POST"])
def leave():
    user, game, body, err = _ctx()
    if err:
        return err
    ok, msg = game.remove_player(int(user["id"]))
    return _view(game, int(user["id"]), {"result": {"ok": ok, "msg": msg}})


@bp.route("/api/mafia/addbot", methods=["POST"])
def addbot():
    user, game, body, err = _ctx()
    if err:
        return err
    if int(user["id"]) != game.host_id:
        return jsonify({"error": "host_only"}), 403
    ok, msg = game.add_bot()
    return _view(game, int(user["id"]), {"result": {"ok": ok, "msg": msg}})


@bp.route("/api/mafia/removebot", methods=["POST"])
def removebot():
    user, game, body, err = _ctx()
    if err:
        return err
    if int(user["id"]) != game.host_id:
        return jsonify({"error": "host_only"}), 403
    ok, msg = game.remove_bot()
    return _view(game, int(user["id"]), {"result": {"ok": ok, "msg": msg}})


@bp.route("/api/mafia/start", methods=["POST"])
def start():
    user, game, body, err = _ctx()
    if err:
        return err
    ok, msg = game.start(int(user["id"]))
    return _view(game, int(user["id"]), {"result": {"ok": ok, "msg": msg}})


@bp.route("/api/mafia/action", methods=["POST"])
def action():
    user, game, body, err = _ctx()
    if err:
        return err
    try:
        target = int(body.get("target"))
        t2 = body.get("target2")
        t2 = int(t2) if t2 is not None else None
    except (TypeError, ValueError):
        return jsonify({"error": "bad_target"}), 400
    ok, msg = game.set_action(int(user["id"]), target, t2)
    return _view(game, int(user["id"]), {"result": {"ok": ok, "msg": msg}})


@bp.route("/api/mafia/vote", methods=["POST"])
def vote():
    user, game, body, err = _ctx()
    if err:
        return err
    ok, msg = game.set_vote(int(user["id"]), body.get("target"))
    return _view(game, int(user["id"]), {"result": {"ok": ok, "msg": msg}})


@bp.route("/api/mafia/ready", methods=["POST"])
def ready():
    user, game, body, err = _ctx()
    if err:
        return err
    ok, msg = game.set_ready(int(user["id"]))
    return _view(game, int(user["id"]), {"result": {"ok": ok, "msg": msg}})


@bp.route("/api/mafia/chat", methods=["POST"])
def chat():
    user, game, body, err = _ctx()
    if err:
        return err
    ok, msg = game.post_chat(int(user["id"]), str(body.get("text", "")))
    return _view(game, int(user["id"]), {"result": {"ok": ok, "msg": msg}})


# ── yangi endpointlar: sozlamalar, oxirgi so'z, reaksiya, veto, reyting, qayta ulanish ──
@bp.route("/api/mafia/settings", methods=["POST"])
def settings():
    user, game, body, err = _ctx()
    if err:
        return err
    patch = body.get("settings") if isinstance(body.get("settings"), dict) else {}
    ok, msg = game.set_settings(int(user["id"]), patch)
    return _view(game, int(user["id"]), {"result": {"ok": ok, "msg": msg}})


@bp.route("/api/mafia/lastword", methods=["POST"])
def lastword():
    user, game, body, err = _ctx()
    if err:
        return err
    ok, msg = game.last_word(int(user["id"]), str(body.get("text", "")))
    return _view(game, int(user["id"]), {"result": {"ok": ok, "msg": msg}})


@bp.route("/api/mafia/react", methods=["POST"])
def react():
    user, game, body, err = _ctx()
    if err:
        return err
    ok, msg = game.react(int(user["id"]), str(body.get("emoji", "")))
    return _view(game, int(user["id"]), {"result": {"ok": ok, "msg": msg}})


@bp.route("/api/mafia/veto", methods=["POST"])
def veto():
    user, game, body, err = _ctx()
    if err:
        return err
    ok, msg = game.use_veto(int(user["id"]))
    return _view(game, int(user["id"]), {"result": {"ok": ok, "msg": msg}})


@bp.route("/api/mafia/mine", methods=["POST"])
def mine():
    """Mini App yopilib qolsa: foydalanuvchi qatnashayotgan o'yinni topib beradi."""
    body = request.get_json(silent=True) or {}
    user = _user(body)
    if not user:
        return jsonify({"error": "auth"}), 401
    g = mafia.find_game_for(int(user["id"]))
    return jsonify({"gid": g.gid if g else None, "phase": g.phase if g else None,
                    "chat_title": g.chat_title if g else None})


@bp.route("/api/mafia/stats", methods=["POST"])
def stats():
    body = request.get_json(silent=True) or {}
    user = _user(body)
    if not user:
        return jsonify({"error": "auth"}), 401
    import mafia_stats
    uid = int(user["id"])
    return jsonify({
        "week": mafia_stats.leaderboard("week", 10),
        "all": mafia_stats.leaderboard("all", 10),
        "me": mafia_stats.profile(uid, user.get("first_name") or ""),
        "week_key": mafia_stats.week_key(),
        "achievements": mafia_stats.ACHIEVEMENTS,
        "roles": mafia.ROLES_PUBLIC,
    })
