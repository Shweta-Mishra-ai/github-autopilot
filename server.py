"""
GitHub Autopilot — Main Server
Flask app that receives GitHub webhooks and manages repos automatically.
"""

import os
import hmac
import hashlib
import json
import logging
from flask import Flask, request, jsonify
from app.handlers import handle_pull_request, handle_issues, handle_issue_comment, handle_push

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)

WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")


def verify_signature(payload: bytes, signature: str) -> bool:
    """Verify GitHub webhook signature."""
    if not WEBHOOK_SECRET:
        return True  # Skip in dev mode
    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "running",
        "app": "GitHub Autopilot",
        "version": "1.0.0",
        "install_url": f"https://github.com/apps/{os.environ.get('GITHUB_APP_SLUG', 'github-autopilot')}/installations/new"
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    """Main webhook endpoint — GitHub sends all events here."""
    # Verify signature
    sig = request.headers.get("X-Hub-Signature-256", "")
    if WEBHOOK_SECRET and not verify_signature(request.data, sig):
        log.warning("Invalid webhook signature")
        return jsonify({"error": "Invalid signature"}), 401

    event = request.headers.get("X-GitHub-Event", "")
    payload = request.get_json(force=True) or {}

    log.info(f"Event: {event} | Repo: {payload.get('repository', {}).get('full_name', 'unknown')}")

    try:
        if event == "pull_request":
            handle_pull_request(payload)
        elif event == "issues":
            handle_issues(payload)
        elif event == "issue_comment":
            handle_issue_comment(payload)
        elif event == "push":
            handle_push(payload)
        elif event == "installation":
            action = payload.get("action")
            account = payload.get("installation", {}).get("account", {}).get("login", "unknown")
            log.info(f"App {action} by: {account}")
        elif event == "ping":
            log.info("Ping received — webhook connected!")
        else:
            log.info(f"Unhandled event: {event}")
    except Exception as e:
        log.error(f"Error handling {event}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True, "event": event}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port, debug=False)
