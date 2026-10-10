"""
Misumi AI — website API (/api/web/*).

Used by the standalone site (Vercel). Provides email+password accounts,
the chat endpoint, and server-side conversation storage so history is the
same on every device. Data lives in Postgres through db.py (DATABASE_URL).

Optional env vars:
  WEB_SECRET  long random string used to sign login tokens
              (falls back to a value derived from TELEGRAM_BOT_TOKEN)
  WEB_ORIGIN  allowed site origin, e.g. https://misumi.vercel.app (default: *)
"""
import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from collections import defaultdict

from flask import Blueprint, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

import admin_store
import ai_core
import db

bp = Blueprint("web_api", __name__, url_prefix="/api/web")

_SECRET = (
    os.environ.get("WEB_SECRET")
    or hashlib.sha256((os.environ.get("TELEGRAM_BOT_TOKEN", "") + "|misumi-web").encode()).hexdigest()
).encode()
_ORIGIN = os.environ.get("WEB_ORIGIN", "*").strip() or "*"
TOKEN_TTL = 30 * 24 * 3600
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
_attempts: dict = defaultdict(list)


@bp.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = _ORIGIN
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, OPTIONS"
    resp.headers["Vary"] = "Origin"
    return resp


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _make_token(uid: str) -> str:
    payload = _b64(json.dumps({"u": uid, "e": int(time.time()) + TOKEN_TTL}).encode())
    sig = _b64(hmac.new(_SECRET, payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def _auth():
    """Return the user record for the Bearer token, or None."""
    h = request.headers.get("Authorization", "")
    if not h.startswith("Bearer "):
        return None
    try:
        payload, sig = h[7:].split(".", 1)
        good = _b64(hmac.new(_SECRET, payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        data = json.loads(_unb64(payload))
        if data["e"] < time.time():
            return None
        return db.load("webuser_id", data["u"], None)
    except Exception:
        return None


def _too_many(key: str, limit: int = 10, window: int = 600) -> bool:
    now = time.time()
    _attempts[key] = [t for t in _attempts[key] if now - t < window]
    if len(_attempts[key]) >= limit:
        return True
    _attempts[key].append(now)
    return False


def _ip() -> str:
    return (request.headers.get("X-Forwarded-For", request.remote_addr or "") or "").split(",")[0].strip()


def _public(u: dict) -> dict:
    return {"name": u["name"], "email": u["email"]}


@bp.route("/register", methods=["POST"])
def register():
    b = request.get_json(silent=True) or {}
    name = (b.get("name") or "").strip()[:40]
    email = (b.get("email") or "").strip().lower()
    pw = b.get("password") or ""
    if _too_many("reg:" + _ip(), limit=8):
        return jsonify({"error": "Juda ko'p urinish. Birozdan keyin qayta urining."}), 429
    if not name:
        return jsonify({"error": "Ismingizni yozing."}), 400
    if not EMAIL_RE.match(email) or len(email) > 120:
        return jsonify({"error": "Email noto'g'ri."}), 400
    if len(pw) < 8 or len(pw) > 128:
        return jsonify({"error": "Parol kamida 8 belgi bo'lsin."}), 400
    if db.exists("webuser", email):
        return jsonify({"error": "Bu email bilan hisob allaqachon bor."}), 409
    uid = "w" + uuid.uuid4().hex[:12]
    user = {"id": uid, "name": name, "email": email, "pw": generate_password_hash(pw), "created": int(time.time())}
    db.save("webuser", email, user)
    db.save("webuser_id", uid, user)
    admin_store.record_message(uid, name, "web")
    return jsonify({"token": _make_token(uid)})


@bp.route("/login", methods=["POST"])
def login():
    b = request.get_json(silent=True) or {}
    email = (b.get("email") or "").strip().lower()
    pw = b.get("password") or ""
    if _too_many("log:" + _ip() + email, limit=10):
        return jsonify({"error": "Juda ko'p urinish. Birozdan keyin qayta urining."}), 429
    user = db.load("webuser", email, None)
    if not user or not check_password_hash(user["pw"], pw):
        return jsonify({"error": "Email yoki parol noto'g'ri."}), 401
    return jsonify({"token": _make_token(user["id"])})


@bp.route("/me", methods=["GET"])
def me():
    u = _auth()
    if not u:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({
        "user": _public(u),
        "premium": bool(admin_store.is_premium(u["id"])),
        "models": list(ai_core.MODEL_TIERS.values()),
    })


@bp.route("/convs", methods=["GET", "PUT"])
def convs():
    u = _auth()
    if not u:
        return jsonify({"error": "Unauthorized"}), 401
    if request.method == "GET":
        return jsonify({"convs": db.load("webconvs", u["id"], [])})
    data = (request.get_json(silent=True) or {}).get("convs")
    if not isinstance(data, list):
        return jsonify({"error": "Noto'g'ri ma'lumot"}), 400
    clean = []
    for c in data[:100]:
        if not isinstance(c, dict):
            continue
        msgs = [
            {"role": m.get("role"), "content": str(m.get("content") or "")[:20000], "fb": m.get("fb")}
            for m in (c.get("msgs") or [])[:400]
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")
        ]
        if msgs:
            clean.append({"id": c.get("id"), "title": str(c.get("title") or "Suhbat")[:60], "msgs": msgs})
    if len(json.dumps(clean)) > 2_000_000:
        return jsonify({"error": "Suhbatlar juda katta"}), 413
    db.save("webconvs", u["id"], clean)
    return jsonify({"ok": True})


@bp.route("/chat", methods=["POST"])
def chat():
    u = _auth()
    if not u:
        return jsonify({"error": "Unauthorized"}), 401
    b = request.get_json(silent=True) or {}
    message = (b.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Xabar bo'sh"}), 400
    uid = u["id"]
    if admin_store.is_blocked(uid):
        return jsonify({"error": "Hisobingiz bloklangan."}), 403
    model = ai_core.resolve_model(uid, b.get("model"))
    if admin_store.is_maintenance():
        return jsonify({"reply": f"🛠 {ai_core.BOT_NAME} hozir texnik ishlar tufayli vaqtincha ishlamayapti.", "model": model})
    if b.get("regenerate"):
        ai_core.pop_last_exchange(uid)
    admin_store.record_message(uid, u["name"], "web")
    try:
        if ai_core.wants_image(message):
            raw, mime = ai_core.generate_image_reply(uid, message, name=u["name"], source="web")
            return jsonify({"reply": "", "generated_image": f"data:{mime};base64,{base64.b64encode(raw).decode()}", "model": model})
        reply = ai_core.get_ai_reply(uid, message, None, None, name=u["name"], source="web", model=model)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"reply": reply, "model": model})
