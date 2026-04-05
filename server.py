"""
server.py — Flask entry point. V4
Webhook receiver only. Acks in < 50ms. Zero AI calls on this path.

V4 changes:
  + Celery task.delay() replaces raw threading
  + IP-based webhook rate limiting (LOOPHOLE 2)
  + Payload size guard — 25MB cap (LOOPHOLE 14)
  + Stronger bot loop prevention — sender.type check (LOOPHOLE 4)
  + /metrics endpoint requires Bearer token (BUG 11)
  + /test-discord debug endpoint
  + Richer /health with circuit breaker + GitHub rate limit status
"""

import hashlib
import hmac
import logging
import os
import time

from flask import Flask, jsonify, request

from app.core.idempotency import is_duplicate, make_fingerprint
from app.core.metrics import metrics
from app.core.redis_client import get_redis, is_redis_available

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("server")

app = Flask(__name__)

WEBHOOK_SECRET    = os.environ.get("GITHUB_WEBHOOK_SECRET", "").encode()
METRICS_TOKEN     = os.environ.get("METRICS_AUTH_TOKEN", "")
MAX_PAYLOAD_BYTES = 25 * 1024 * 1024   # 25 MB — GitHub's own limit
START_TIME        = time.time()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _verify_signature(payload_bytes: bytes, signature: str) -> bool:
    if not WEBHOOK_SECRET:
        log.warning("GITHUB_WEBHOOK_SECRET not set — skipping signature verification")
        return True
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET, payload_bytes, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _check_ip_rate_limit(ip: str) -> bool:
    """
    LOOPHOLE 2: Limit each IP to 100 webhook requests per minute.
    Prevents flooding before signature check CPU cost.
    Returns True if allowed, False if blocked.
    """
    try:
        r   = get_redis()
        key = f"webhook_rl:{ip}:{int(time.time() // 60)}"
        count = r.incr(key)
        r.expire(key, 60)
        return int(count) <= 100
    except Exception:
        return True  # If Redis unavailable, allow through


def _get_tasks():
    """Lazy import — prevents Celery connecting at import time."""
    import app.tasks as t
    return t


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "app":     "AI Repo Manager",
        "version": "4.0.0",
        "status":  "running",
        "docs":    "https://github.com/Shweta-Mishra-ai/github-autopilot",
    })


@app.route("/health", methods=["GET"])
def health():
    """
    UptimeRobot-ready health endpoint.
    Returns 200 when ok/degraded, 207 when partially down.
    Checks: Redis, GitHub rate limit, all LLM circuit breakers.
    """
    from app.ai.circuit_breaker import status_all
    from app.github.rate_limit import get_status as gh_rl_status

    redis_ok      = is_redis_available()
    gh_status     = gh_rl_status()
    gh_ok         = gh_status.get("remaining", 5000) > 50
    breaker_status = status_all()
    any_llm_ok    = any(
        s["state"] == "closed" for s in breaker_status.values()
    )

    overall = "ok" if (redis_ok and gh_ok and any_llm_ok) else "degraded"

    return jsonify({
        "status":         overall,
        "version":        "4.0.0",
        "uptime_seconds": int(time.time() - START_TIME),
        "checks": {
            "redis":        "ok" if redis_ok else "unavailable",
            "github_api":   "ok" if gh_ok else "rate_limited",
            "llm_providers": breaker_status,
        },
        "metrics": {
            "events_total":  metrics.get("events.total", 0),
            "errors_total":  metrics.get("events.error", 0),
            "queued_total":  metrics.get("events.queued", 0),
        },
    }), 200 if overall == "ok" else 207


@app.route("/metrics", methods=["GET"])
def get_metrics():
    """
    Internal metrics endpoint.
    FIXED (BUG 11): Requires Bearer token if METRICS_AUTH_TOKEN is set.
    """
    if METRICS_TOKEN:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {METRICS_TOKEN}":
            return jsonify({"error": "Unauthorized"}), 401

    return jsonify(metrics.snapshot())


@app.route("/test-discord", methods=["POST"])
def test_discord():
    """
    NEW V4: Manually test Discord webhook.
    Only available outside production.
    curl -X POST https://your-app.onrender.com/test-discord
    """
    if os.environ.get("FLASK_ENV") == "production":
        return jsonify({"error": "Not available in production"}), 403

    from app.github.notifications import test_discord as _test
    success, message = _test()
    status_code = 200 if success else 500
    return jsonify({"success": success, "message": message}), status_code


@app.route("/webhook", methods=["POST"])
def webhook():
    # ── LOOPHOLE 14: Payload size guard ──────────────────────────────────────
    content_length = request.content_length
    if content_length and content_length > MAX_PAYLOAD_BYTES:
        log.warning(f"webhook.payload_too_large bytes={content_length}")
        metrics.increment("webhook.rejected.too_large")
        return jsonify({"error": "Payload too large"}), 413

    # ── LOOPHOLE 2: IP rate limit ─────────────────────────────────────────────
    client_ip = (
        request.headers.get("X-Forwarded-For", request.remote_addr or "")
        .split(",")[0]
        .strip()
    )
    if not _check_ip_rate_limit(client_ip):
        log.warning(f"webhook.rate_limited ip={client_ip}")
        metrics.increment("webhook.rejected.rate_limited")
        return jsonify({"error": "Too many requests"}), 429

    # ── Signature verification ────────────────────────────────────────────────
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_signature(request.data, sig):
        log.warning("webhook.invalid_signature")
        metrics.increment("webhook.rejected.invalid_signature")
        return jsonify({"error": "Invalid signature"}), 401

    # ── Parse payload ─────────────────────────────────────────────────────────
    try:
        payload = request.get_json(force=True)
    except Exception:
        metrics.increment("webhook.rejected.invalid_json")
        return jsonify({"error": "Invalid JSON"}), 400

    webhook_event = request.headers.get("X-GitHub-Event", "")
    delivery_id   = request.headers.get("X-GitHub-Delivery", "")
    repo = payload.get("repository", {}).get("full_name", "unknown")

    # ── LOOPHOLE 4: Bot loop prevention ──────────────────────────────────────
    # V3 only checked login string. V4 also checks sender.type == "Bot".
    sender       = payload.get("sender", {})
    sender_type  = sender.get("type", "")
    sender_login = sender.get("login", "")
    if sender_type == "Bot" or sender_login.endswith("[bot]"):
        log.debug(f"webhook.skipped bot_sender={sender_login}")
        return jsonify({"status": "skipped — bot sender"}), 200

    log.info(
        f"webhook.received event={webhook_event} "
        f"repo={repo} delivery={delivery_id[:8]}"
    )
    metrics.increment("webhook.received")

    # ── Idempotency check ─────────────────────────────────────────────────────
    fingerprint = make_fingerprint(delivery_id, webhook_event, payload)
    if is_duplicate(fingerprint):
        log.info(f"webhook.duplicate fingerprint={fingerprint}")
        metrics.increment("webhook.duplicate_skipped")
        return jsonify({"status": "duplicate — skipped"}), 200

    # ── Route to Celery task ──────────────────────────────────────────────────
    tasks = _get_tasks()

    task_map = {
        "pull_request":  tasks.handle_pull_request,
        "issues":        tasks.handle_issue,
        "issue_comment": tasks.handle_issue_comment,
        "push":          tasks.handle_push,
        "check_run":     tasks.handle_check_run,
    }

    task = task_map.get(webhook_event)
    if task:
        task.delay(payload)
        metrics.increment(f"events.{webhook_event}.queued")
        metrics.increment("events.total")
        log.info(f"webhook.queued event={webhook_event} repo={repo}")
    else:
        log.debug(f"webhook.unhandled event={webhook_event}")

    # Always ack immediately — Celery handles the rest
    return jsonify({"status": "accepted"}), 202


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
