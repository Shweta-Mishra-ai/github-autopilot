"""
server.py — Flask entry point. V4
Webhook receiver. Acks in < 50ms.

FREE TIER FIX: Celery worker runs as separate Render service.
On free tier, if Celery broker is not reachable, falls back to threading
so the app works with just one Render service (web only).

Push events work (confirmed). Issue_comment (slash commands) need
Celery worker OR threading fallback — this file handles both.
"""

import hashlib
import hmac
import logging
import os
import time
import threading

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
MAX_PAYLOAD_BYTES = 25 * 1024 * 1024
START_TIME        = time.time()

# Set to True if Celery broker confirmed reachable at startup
_celery_available = False


def _check_celery():
    """Check once at startup if Celery broker (Redis) is reachable."""
    global _celery_available
    try:
        if not is_redis_available():
            _celery_available = False
            log.warning("server.celery_unavailable — using threading fallback")
            return
        # Try importing and pinging Celery
        from app.celery_app import celery
        celery.control.inspect(timeout=1).ping()
        _celery_available = True
        log.info("server.celery_available — tasks will use Celery queue")
    except Exception as e:
        _celery_available = False
        log.warning(f"server.celery_unavailable reason={e} — using threading fallback")


# Check Celery at startup (non-blocking)
threading.Thread(target=_check_celery, daemon=True).start()


def _verify_signature(payload_bytes: bytes, signature: str) -> bool:
    if not WEBHOOK_SECRET:
        log.warning("GITHUB_WEBHOOK_SECRET not set — skipping verification")
        return True
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET, payload_bytes, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _check_ip_rate_limit(ip: str) -> bool:
    try:
        r   = get_redis()
        key = f"webhook_rl:{ip}:{int(time.time() // 60)}"
        count = r.incr(key)
        r.expire(key, 60)
        return int(count) <= 100
    except Exception:
        return True


def _dispatch_via_thread(webhook_event: str, payload: dict, repo: str):
    """
    Threading fallback when Celery worker is not running.
    Same as V3 approach — works on single free tier service.
    """
    def _run():
        try:
            log.info(f"thread.processing event={webhook_event} repo={repo}")
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
            elif webhook_event == "check_run":
                from app.handlers.ci import handle
                handle(payload)
            metrics.increment(f"events.{webhook_event}.success")
            log.info(f"thread.done event={webhook_event}")
        except Exception as e:
            import traceback
            log.error(f"thread.error event={webhook_event}: {e}")
            log.error(traceback.format_exc())
            metrics.increment(f"events.{webhook_event}.error")

    t = threading.Thread(target=_run, daemon=False)
    t.start()


def _dispatch_via_celery(webhook_event: str, payload: dict) -> bool:
    """Try to dispatch via Celery. Returns False if fails."""
    try:
        from app import tasks
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
            return True
        return False
    except Exception as e:
        log.warning(f"celery.dispatch_failed event={webhook_event}: {e}")
        return False


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "app":     "AI Repo Manager",
        "version": "4.0.0",
        "status":  "running",
        "worker":  "celery" if _celery_available else "threading",
        "docs":    "https://github.com/Shweta-Mishra-ai/github-autopilot",
    })


@app.route("/health", methods=["GET"])
def health():
    from app.ai.circuit_breaker import status_all
    from app.github.rate_limit import get_status as gh_rl_status

    redis_ok       = is_redis_available()
    gh_status      = gh_rl_status()
    gh_ok          = gh_status.get("remaining", 5000) > 50
    breaker_status = status_all()
    any_llm_ok     = any(s["state"] == "closed" for s in breaker_status.values())

    overall = "ok" if (gh_ok and any_llm_ok) else "degraded"

    return jsonify({
        "status":         overall,
        "version":        "4.0.0",
        "uptime_seconds": int(time.time() - START_TIME),
        "worker_mode":    "celery" if _celery_available else "threading",
        "checks": {
            "redis":         "ok" if redis_ok else "unavailable",
            "celery_worker": "ok" if _celery_available else "fallback_threading",
            "github_api":    "ok" if gh_ok else "rate_limited",
            "llm_providers": breaker_status,
        },
        "metrics": {
            "events_total": metrics.get("events.total", 0),
            "errors_total": metrics.get("events.error", 0),
        },
    }), 200 if overall == "ok" else 207


@app.route("/metrics", methods=["GET"])
def get_metrics():
    if METRICS_TOKEN:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {METRICS_TOKEN}":
            return jsonify({"error": "Unauthorized"}), 401
    return jsonify(metrics.snapshot())


@app.route("/test-discord", methods=["POST"])
def test_discord():
    if os.environ.get("FLASK_ENV") == "production":
        return jsonify({"error": "Not available in production"}), 403
    from app.github.notifications import test_discord as _test
    success, message = _test()
    return jsonify({"success": success, "message": message}), 200 if success else 500


@app.route("/webhook", methods=["POST"])
def webhook():
    # Payload size guard
    content_length = request.content_length
    if content_length and content_length > MAX_PAYLOAD_BYTES:
        log.warning(f"webhook.payload_too_large bytes={content_length}")
        return jsonify({"error": "Payload too large"}), 413

    # IP rate limit
    client_ip = (
        request.headers.get("X-Forwarded-For", request.remote_addr or "")
        .split(",")[0].strip()
    )
    if not _check_ip_rate_limit(client_ip):
        log.warning(f"webhook.rate_limited ip={client_ip}")
        return jsonify({"error": "Too many requests"}), 429

    # Signature check
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_signature(request.data, sig):
        log.warning("webhook.invalid_signature")
        return jsonify({"error": "Invalid signature"}), 401

    # Parse JSON
    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    webhook_event = request.headers.get("X-GitHub-Event", "")
    delivery_id   = request.headers.get("X-GitHub-Delivery", "")
    repo = payload.get("repository", {}).get("full_name", "unknown")

    # Bot loop prevention
    sender       = payload.get("sender", {})
    sender_type  = sender.get("type", "")
    sender_login = sender.get("login", "")
    if sender_type == "Bot" or sender_login.endswith("[bot]"):
        log.debug(f"webhook.skipped bot_sender={sender_login}")
        return jsonify({"status": "skipped — bot sender"}), 200

    log.info(f"webhook.received event={webhook_event} repo={repo} delivery={delivery_id[:8]}")
    metrics.increment("webhook.received")

    # Idempotency
    fingerprint = make_fingerprint(delivery_id, webhook_event, payload)
    if is_duplicate(fingerprint):
        log.info(f"webhook.duplicate fingerprint={fingerprint}")
        metrics.increment("webhook.duplicate_skipped")
        return jsonify({"status": "duplicate — skipped"}), 200

    # Dispatch: Celery if available, threading fallback otherwise
    known_events = {"pull_request", "issues", "issue_comment", "push", "check_run"}
    if webhook_event not in known_events:
        log.debug(f"webhook.unhandled event={webhook_event}")
        return jsonify({"status": "accepted"}), 202

    dispatched = False
    if _celery_available:
        dispatched = _dispatch_via_celery(webhook_event, payload)

    if not dispatched:
        # Threading fallback — works on single free tier service
        _dispatch_via_thread(webhook_event, payload, repo)
        log.info(f"webhook.dispatched_thread event={webhook_event} repo={repo}")
    else:
        log.info(f"webhook.dispatched_celery event={webhook_event} repo={repo}")

    metrics.increment(f"events.{webhook_event}.queued")
    metrics.increment("events.total")
    return jsonify({"status": "accepted"}), 202


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
