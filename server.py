"""
server.py — Flask entry point. V3
Webhook ingestion ONLY. No processing here.
All events are enqueued and processed by workers.
"""

import os
import hmac
import hashlib
import logging
from flask import Flask, request, jsonify

from app.core.metrics import metrics
from app.core.idempotency import make_fingerprint, is_duplicate
from app.queue.producer import enqueue_event

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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

    event_type = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    repo = payload.get("repository", {}).get("full_name", "unknown")

    log.info(f"Webhook received: {event_type} | {repo} | {delivery_id[:8]}")
    metrics.increment("webhook.received")

    fingerprint = make_fingerprint(delivery_id, event_type, payload)
    if is_duplicate(fingerprint):
        log.info(f"Duplicate skipped: {fingerprint}")
        metrics.increment("webhook.duplicate_skipped")
        return jsonify({"status": "duplicate — skipped"}), 200

    enqueue_event(event_type, payload, delivery_id)
    metrics.increment("events.total")

    return jsonify({"status": "accepted"}), 202


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
