"""
server.py — Flask entry point. V3
Webhook ingestion + background thread processing.
Redis available hone par queue-based processing use karo.
Free tier pe: in-memory queue + background thread.
"""

import os
import hmac
import hashlib
import threading
import logging
from flask import Flask, request, jsonify

from app.core.metrics import metrics
from app.core.idempotency import make_fingerprint, is_duplicate
from app.queue.producer import enqueue_event

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
log = logging.getLogger("server")

app = Flask(__name__)
WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "").encode()

# Start background worker thread on startup
def _start_worker():
    from app.queue.consumer import consume_events
    for webhook_event, payload in consume_events():
        try:
            _dispatch(webhook_event, payload)
            metrics.increment(f"events.{webhook_event}.success")
        except Exception as e:
            log.error(f"Dispatch failed: {webhook_event} — {e}")
            metrics.increment(f"events.{webhook_event}.error")

def _dispatch(webhook_event: str, payload: dict):
    if webhook_event == "pull_request":
        from app.handlers.pull_request import handle
        handle(payload)
    elif webhook_event == "issues":
        from app.handlers.issues import handle
        handle(payload)
    elif webhook_event == "issue_comment":
        from app.handlers.comments import handle
        handle(payload)
    elif webhook_event == "push":
        from app.handlers.push import handle
        handle(payload)
    else:
        log.debug(f"Unhandled event: {webhook_event}")

# Start worker thread as daemon
_worker_thread = threading.Thread(target=_start_worker, daemon=True)
_worker_thread.start()
log.info("Background worker thread started")


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
        "version": "3.0.0",
        "status": "running",
        "events_processed": metrics.get("events.total", 0),
    })


@app.route("/metrics", methods=["GET"])
def get_metrics():
    return jsonify(metrics.snapshot())


@app.route("/webhook", methods=["POST"])
def webhook():
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_signature(request.data, sig):
        log.warning("Invalid webhook signature")
        metrics.increment("webhook.rejected.invalid_signature")
        return jsonify({"error": "Invalid signature"}), 401

    try:
        payload = request.get_json(force=True)
    except Exception:
        metrics.increment("webhook.rejected.invalid_json")
        return jsonify({"error": "Invalid JSON"}), 400

    webhook_event = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    repo = payload.get("repository", {}).get("full_name", "unknown")

    log.info(f"Webhook received: {webhook_event} | {repo} | {delivery_id[:8]}")
    metrics.increment("webhook.received")

    fingerprint = make_fingerprint(delivery_id, webhook_event, payload)
    if is_duplicate(fingerprint):
        log.info(f"Duplicate skipped: {fingerprint}")
        metrics.increment("webhook.duplicate_skipped")
        return jsonify({"status": "duplicate — skipped"}), 200

    enqueue_event(webhook_event, payload, delivery_id)
    metrics.increment("events.total")

    return jsonify({"status": "accepted"}), 202


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
