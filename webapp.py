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

import admin_store
import ai_core

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID = os.environ.get("ADMIN_ID", "").strip()

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


def verify_admin(init_data: str) -> dict | None:
    """Like verify_init_data, but also requires the user's id to match
    ADMIN_ID. Returns None if unauthenticated, not the admin, or ADMIN_ID
    isn't configured at all.
    """
    if not ADMIN_ID:
        return None
    data = verify_init_data(init_data)
    if not data:
        return None
    user = data.get("user", {})
    if str(user.get("id", "")) != ADMIN_ID:
        return None
    return data


@app.route("/")
def index():
    return render_template(
        "index.html",
        bot_name=ai_core.BOT_NAME,
        author=ai_core.AUTHOR_HANDLE,
        channel=ai_core.CHANNEL_HANDLE,
        model_tiers_json=json.dumps(list(ai_core.MODEL_TIERS.values())),
        default_model=ai_core.DEFAULT_MODEL,
    )


@app.route("/admin")
def admin_page():
    return render_template("admin.html", bot_name=ai_core.BOT_NAME)


@app.route("/api/chat", methods=["POST"])
def api_chat():
    body = request.get_json(silent=True) or {}
    init_data = body.get("initData", "")
    message = (body.get("message") or "").strip()
    image_data_url = body.get("image")  # optional: "data:image/jpeg;base64,...."
    requested_model = body.get("model")  # "flash" | "pro" | "max"

    data = verify_init_data(init_data)
    if not data:
        return jsonify({"error": "Unauthorized"}), 401
    if not message and not image_data_url:
        return jsonify({"error": "Empty message"}), 400

    user = data.get("user", {})
    user_id = user.get("id")
    if not user_id:
        return jsonify({"error": "No user id"}), 400

    if admin_store.is_blocked(user_id):
        return jsonify({"error": "Blocked"}), 403

    # Resolve the model tier server-side so a free user can't just send
    # {"model": "max"} to bypass premium gating.
    effective_model = ai_core.resolve_model(str(user_id), requested_model)

    if admin_store.is_maintenance() and str(user_id) != ADMIN_ID:
        return jsonify(
            {"reply": f"🛠 {ai_core.BOT_NAME} hozir texnik ishlar tufayli vaqtincha ishlamayapti."}
        )

    image_bytes = None
    image_mime = None
    if image_data_url and image_data_url.startswith("data:"):
        try:
            header, b64data = image_data_url.split(",", 1)
            image_mime = header.split(";")[0].replace("data:", "") or "image/jpeg"
            image_bytes = base64.b64decode(b64data)
        except Exception:
            return jsonify({"error": "Invalid image"}), 400

    # If the user is asking us to *generate* an image (and isn't attaching
    # one for us to look at), route to image generation instead of chat.
    if not image_bytes and ai_core.wants_image(message):
        try:
            gen_bytes, gen_mime = ai_core.generate_image_reply(
                str(user_id), message, name=user.get("first_name"), source="miniapp"
            )
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        gen_b64 = base64.b64encode(gen_bytes).decode("ascii")
        return jsonify({
            "reply": "",
            "generated_image": f"data:{gen_mime};base64,{gen_b64}",
            "model": effective_model,
        })

    try:
        reply = ai_core.get_ai_reply(
            str(user_id),
            message,
            image_bytes,
            image_mime,
            name=user.get("first_name"),
            source="miniapp",
            model=effective_model,
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"reply": reply, "model": effective_model})


@app.route("/api/me", methods=["POST"])
def api_me():
    """Tells the Mini App whether the current user has premium, so it can
    show/hide the lock icon on the Pro/Max model options without the
    client having to guess or trust its own local state.
    """
    body = request.get_json(silent=True) or {}
    data = verify_init_data(body.get("initData", ""))
    if not data:
        return jsonify({"error": "Unauthorized"}), 401
    user = data.get("user", {})
    user_id = user.get("id")
    premium = admin_store.is_premium(user_id) if user_id else False
    return jsonify({"premium": bool(premium), "models": list(ai_core.MODEL_TIERS.values())})


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


@app.route("/api/admin/stats", methods=["POST"])
def api_admin_stats():
    body = request.get_json(silent=True) or {}
    if not verify_admin(body.get("initData", "")):
        return jsonify({"error": "Unauthorized"}), 403
    return jsonify(admin_store.get_stats())


@app.route("/api/admin/users", methods=["POST"])
def api_admin_users():
    body = request.get_json(silent=True) or {}
    if not verify_admin(body.get("initData", "")):
        return jsonify({"error": "Unauthorized"}), 403
    return jsonify({"users": admin_store.get_users()})


@app.route("/api/admin/toggle-block", methods=["POST"])
def api_admin_toggle_block():
    body = request.get_json(silent=True) or {}
    if not verify_admin(body.get("initData", "")):
        return jsonify({"error": "Unauthorized"}), 403
    target_id = str(body.get("user_id", "")).strip()
    if not target_id:
        return jsonify({"error": "user_id required"}), 400
    if target_id == ADMIN_ID:
        return jsonify({"error": "Can't block the admin"}), 400
    blocked = admin_store.toggle_block(target_id)
    return jsonify({"user_id": target_id, "blocked": blocked})


@app.route("/api/admin/toggle-premium", methods=["POST"])
def api_admin_toggle_premium():
    body = request.get_json(silent=True) or {}
    if not verify_admin(body.get("initData", "")):
        return jsonify({"error": "Unauthorized"}), 403
    target_id = str(body.get("user_id", "")).strip()
    if not target_id:
        return jsonify({"error": "user_id required"}), 400
    premium = admin_store.toggle_premium(target_id)
    return jsonify({"user_id": target_id, "premium": premium})


@app.route("/api/admin/maintenance", methods=["POST"])
def api_admin_maintenance():
    body = request.get_json(silent=True) or {}
    if not verify_admin(body.get("initData", "")):
        return jsonify({"error": "Unauthorized"}), 403
    value = admin_store.set_maintenance(bool(body.get("value")))
    return jsonify({"maintenance": value})


@app.route("/health")
def health():
    return "Misumi AI is running", 200


def run(port: int):
    # threaded=True lets the health check and chat requests be handled
    # concurrently without blocking each other.
    app.run(host="0.0.0.0", port=port, threaded=True)
