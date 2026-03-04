"""
server.py — Flask entry point.
This file ONLY does: routing, signature verification, idempotency check.
All business logic lives in app/handlers/*.
"""

import os
import hmac
import hashlib
import logging
from flask import Flask, request, jsonify

from app.core.logger import setup_logging
from app.core.idempotency import make_fingerprint, is_duplicate

setup_logging()
log = logging.getLogger(__name__)

app = Flask(__name__)

WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "").encode()


def _verify_signature(payload_bytes: bytes, signature: str) -> bool:
    """Verify GitHub webhook HMAC signature."""
    if not WEBHOOK_SECRET:
        log.warning("GITHUB_WEBHOOK_SECRET not set — skipping signature verification")
        return True
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(WEBHOOK_SECRET, payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.route("/", methods=["GET"])
def health():
    return jsonify({"app": "AI Repo Manager", "status": "running", "version": "2.0.0"})


@app.route("/webhook", methods=["POST"])
def webhook():
    # 1. Verify signature
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_signature(request.data, sig):
        log.warning("Invalid webhook signature — rejecting")
        return jsonify({"error": "Invalid signature"}), 401

    # 2. Parse payload
    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    event_type = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    repo = payload.get("repository", {}).get("full_name", "unknown")

    log.info(f"Event: {event_type} | Repo: {repo} | Delivery: {delivery_id}")

    # 3. Idempotency check
    fingerprint = make_fingerprint(delivery_id, event_type, payload)
    if is_duplicate(fingerprint):
        return jsonify({"status": "duplicate — skipped"}), 200

    # 4. Dispatch to handler
    try:
        _dispatch(event_type, payload)
    except Exception as e:
        log.error(f"Handler error [{event_type}] {repo}: {e}", exc_info=True)
        # Return 200 so GitHub doesn't retry — we log internally
        return jsonify({"status": "error", "detail": str(e)[:200]}), 200

    return jsonify({"status": "ok"}), 200


def _dispatch(event_type: str, payload: dict):
    """Route event to the correct handler module."""
    if event_type == "pull_request":
        from app.handlers.pull_request import handle
        handle(payload)

    elif event_type == "issues":
        from app.handlers.issues import handle
        handle(payload)

    elif event_type == "issue_comment":
        from app.handlers.comments import handle
        handle(payload)

    elif event_type == "push":
        from app.handlers.push import handle
        handle(payload)

    else:
        log.debug(f"Unhandled event type: {event_type}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
