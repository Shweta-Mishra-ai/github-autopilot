"""
server.py — Flask entry point. v2.1
Changes from v2.0:
  - Async webhook dispatch (threading) — respond in <1s, process in background
  - /metrics endpoint — observability
  - /health endpoint — system status
"""

import os
import hmac
import hashlib
import logging
import threading
from flask import Flask, request, jsonify

from app.core.logger import setup_logging
from app.core.idempotency import make_fingerprint, is_duplicate
from app.core.metrics import metrics

setup_logging()
log = logging.getLogger(__name__)

app = Flask(__name__)

WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "").encode()


def _verify_signature(payload_bytes: bytes, signature: str) -> bool:
    if not WEBHOOK_SECRET:
        log.warning("GITHUB_WEBHOOK_SECRET not set — skipping verification")
        return True
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(WEBHOOK_SECRET, payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "app": "AI Repo Manager",
        "status": "running",
        "version": "2.1.0",
        "events_processed": metrics.get("events.total", 0),
        "uptime": "check /metrics for details"
    })


@app.route("/metrics", methods=["GET"])
def get_metrics():
    """Observability endpoint — shows system activity counters."""
    return jsonify(metrics.snapshot())


@app.route("/webhook", methods=["POST"])
def webhook():
    # 1. Verify signature
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_signature(request.data, sig):
        log.warning("Invalid webhook signature — rejecting")
        metrics.increment("webhook.rejected.invalid_signature")
        return jsonify({"error": "Invalid signature"}), 401

    # 2. Parse payload
    try:
        payload = request.get_json(force=True)
    except Exception:
        metrics.increment("webhook.rejected.invalid_json")
        return jsonify({"error": "Invalid JSON"}), 400

    event_type = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    repo = payload.get("repository", {}).get("full_name", "unknown")

    log.info(f"Event: {event_type} | Repo: {repo} | Delivery: {delivery_id[:8]}")
    metrics.increment("webhook.received")
    metrics.increment(f"webhook.events.{event_type}")

    # 3. Idempotency check
    fingerprint = make_fingerprint(delivery_id, event_type, payload)
    if is_duplicate(fingerprint):
        log.info(f"Duplicate event skipped: {fingerprint}")
        metrics.increment("webhook.duplicate_skipped")
        return jsonify({"status": "duplicate — skipped"}), 200

    # 4. ASYNC dispatch — respond immediately, process in background
    # This ensures GitHub receives 200 within 1 second
    # Eliminates webhook timeout and retry storms
    thread = threading.Thread(
        target=_safe_dispatch,
        args=(event_type, payload, repo),
        daemon=True
    )
    thread.start()

    metrics.increment("events.total")
    return jsonify({"status": "accepted"}), 202


def _safe_dispatch(event_type: str, payload: dict, repo: str):
    """Runs in background thread. Catches all exceptions."""
    try:
        _dispatch(event_type, payload)
        metrics.increment(f"events.{event_type}.success")
    except Exception as e:
        log.error(f"Handler error [{event_type}] {repo}: {e}", exc_info=True)
        metrics.increment(f"events.{event_type}.error")


def _dispatch(event_type: str, payload: dict):
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
