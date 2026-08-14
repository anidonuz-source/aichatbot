"""
Misumi AI — Telegram Mini App server.

Serves the premium web chat UI (templates/index.html) and a /api/chat
endpoint that the Mini App's JS calls. Requests are authenticated using
Telegram's official WebApp initData verification algorithm, so we know
for certain which Telegram user is talking — no separate login needed.

Docs: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""
import base64
import hashlib
import hmac
import json
import os
from urllib.parse import parse_qsl

from flask import Flask, jsonify, render_template, request

import ai_core

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

app = Flask(__name__)


def verify_init_data(init_data: str) -> dict | None:
    """Validate Telegram WebApp initData. Returns the parsed data dict
    (including 'user') if valid, or None if the signature doesn't match.
    """
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    if "user" in pairs:
        pairs["user"] = json.loads(pairs["user"])
    return pairs


@app.route("/")
def index():
    return render_template("index.html", bot_name=ai_core.BOT_NAME, author=ai_core.AUTHOR_HANDLE)


@app.route("/api/chat", methods=["POST"])
def api_chat():
    body = request.get_json(silent=True) or {}
    init_data = body.get("initData", "")
    message = (body.get("message") or "").strip()
    image_data_url = body.get("image")  # optional: "data:image/jpeg;base64,...."

    data = verify_init_data(init_data)
    if not data:
        return jsonify({"error": "Unauthorized"}), 401
    if not message and not image_data_url:
        return jsonify({"error": "Empty message"}), 400

    user = data.get("user", {})
    user_id = user.get("id")
    if not user_id:
        return jsonify({"error": "No user id"}), 400

    image_bytes = None
    image_mime = None
    if image_data_url and image_data_url.startswith("data:"):
        try:
            header, b64data = image_data_url.split(",", 1)
            image_mime = header.split(";")[0].replace("data:", "") or "image/jpeg"
            image_bytes = base64.b64decode(b64data)
        except Exception:
            return jsonify({"error": "Invalid image"}), 400

    try:
        reply = ai_core.get_ai_reply(str(user_id), message, image_bytes, image_mime)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"reply": reply})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    body = request.get_json(silent=True) or {}
    init_data = body.get("initData", "")
    data = verify_init_data(init_data)
    if not data:
        return jsonify({"error": "Unauthorized"}), 401
    user = data.get("user", {})
    user_id = user.get("id")
    if user_id:
        ai_core.reset_user(str(user_id))
    return jsonify({"ok": True})


@app.route("/health")
def health():
    return "Misumi AI is running", 200


def run(port: int):
    # threaded=True lets the health check and chat requests be handled
    # concurrently without blocking each other.
    app.run(host="0.0.0.0", port=port, threaded=True)
