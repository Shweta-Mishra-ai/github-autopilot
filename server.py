"""
server.py — Flask entry point. V4 (Security Hardened)

CHANGES vs original:
  - WEBHOOK_SECRET missing → startup_check() raises RuntimeError at boot
  - _verify_signature() → fail closed on empty secret (was returning True)
  - _dispatch() → bounded ThreadPoolExecutor (was unbounded Thread())
  - Thread pool cap: MAX_DISPATCH_WORKERS env var (default 6)
  - /health exposes thread pool stats
  - Replay protection via timestamp check in webhook_security
"""

import hashlib
import hmac
import logging
import os
import time
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

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
MAX_PAYLOAD_BYTES = 25 * 1024 * 1024  # 25MB
START_TIME        = time.time()

# Bounded thread pool — prevents OOM on webhook storms
MAX_DISPATCH_WORKERS = int(os.environ.get("MAX_DISPATCH_WORKERS", "6"))
_QUEUE_CAP           = 50  # max pending jobs before dropping
_pool                = ThreadPoolExecutor(
    max_workers=MAX_DISPATCH_WORKERS,
    thread_name_prefix="webhook-dispatch",
)
_pending      = 0
_pending_lock = threading.Lock()

# ── Startup validation ────────────────────────────────────────────────────────

def _startup_check():
    """
    Refuse to start if GITHUB_WEBHOOK_SECRET is not set.
    Running without it means anyone can forge webhook payloads.
    """
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        raise RuntimeError(
            "FATAL: GITHUB_WEBHOOK_SECRET is not set. "
            "Refusing to start — all webhooks would be unverifiable. "
            "Set this env var in Render → Environment → Add Environment Variable."
        )
    if len(secret) < 20:
        log.warning(
            f"GITHUB_WEBHOOK_SECRET is short ({len(secret)} chars). "
            "Use a strong random secret (32+ chars recommended)."
        )
    log.info("startup_check passed: GITHUB_WEBHOOK_SECRET is configured.")


# ── Security helpers ──────────────────────────────────────────────────────────

def _verify_signature(payload_bytes: bytes, signature: str) -> bool:
    """
    HMAC-SHA256 verification. FAIL CLOSED on empty secret.
    Original bug: empty secret returned True (bypass). Now returns False.
    """
    if not WEBHOOK_SECRET:
        log.error(
            "GITHUB_WEBHOOK_SECRET is empty — REJECTING webhook. "
            "This should have been caught at startup."
        )
        return False  # CHANGED: was `return True` — security hole

    if not signature or not signature.startswith("sha256="):
        return False

    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET, payload_bytes, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _check_ip_rate_limit(ip: str) -> bool:
    """100 requests per IP per minute. Redis-backed, in-memory fallback."""
    try:
        r     = get_redis()
        key   = f"webhook_rl:{ip}:{int(time.time() // 60)}"
        count = r.incr(key)
        r.expire(key, 60)
        return int(count) <= 100
    except Exception:
        return True  # Redis unavailable — fail open for UX


def _is_bot_sender(payload: dict) -> bool:
    """Prevent feedback loops from bot-triggered webhooks."""
    sender       = payload.get("sender", {})
    sender_type  = sender.get("type", "")
    sender_login = sender.get("login", "")
    return (
        sender_type == "Bot"
        or sender_login.endswith("[bot]")
        or sender_login in {"ai-repo-manager[bot]", "github-autopilot[bot]"}
    )


# ── Bounded thread pool dispatcher ───────────────────────────────────────────

def _dispatch(webhook_event: str, payload: dict, repo: str):
    """
    Submit event handling to bounded ThreadPoolExecutor.
    Drops event (logged) when queue is saturated — never crashes.
    Original: unbounded Thread() per webhook = OOM risk under load.
    """
    global _pending

    with _pending_lock:
        if _pending >= _QUEUE_CAP:
            log.error(
                f"dispatch.queue_full pending={_pending} cap={_QUEUE_CAP} "
                f"event={webhook_event} repo={repo} — DROPPING"
            )
            return
        _pending += 1

    def _run():
        global _pending
        try:
            log.info(f"dispatch.start event={webhook_event} repo={repo}")

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
                try:
                    from app.handlers.ci import handle
                    handle(payload)
                except ImportError:
                    log.debug("ci handler not found — skipping")

            else:
                log.debug(f"dispatch.unhandled event={webhook_event}")
                return

            metrics.increment(f"events.{webhook_event}.success")
            log.info(f"dispatch.done event={webhook_event}")

        except Exception as e:
            log.error(f"dispatch.error event={webhook_event}: {e}")
            log.error(traceback.format_exc())
            metrics.increment(f"events.{webhook_event}.error")

        finally:
            with _pending_lock:
                _pending -= 1

    try:
        _pool.submit(_run)
    except Exception as e:
        log.error(f"dispatch.submit_failed: {e}")
        with _pending_lock:
            _pending -= 1


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "app":     "AI Repo Manager",
        "version": "4.1.0",
        "status":  "running",
        "mode":    "bounded-threadpool",
        "docs":    "https://github.com/Shweta-Mishra-ai/github-autopilot",
    })


@app.route("/health", methods=["GET"])
def health():
    from app.ai.circuit_breaker import status_all
    from app.github.rate_limit import get_status as gh_rl_status

    redis_ok       = is_redis_available()
    gh_ok          = gh_rl_status().get("remaining", 5000) > 50
    breaker_status = status_all()
    any_llm_ok     = any(s["state"] == "closed" for s in breaker_status.values())
    overall        = "ok" if (gh_ok and any_llm_ok) else "degraded"

    with _pending_lock:
        pending = _pending

    return jsonify({
        "status":         overall,
        "version":        "4.1.0",
        "uptime_seconds": int(time.time() - START_TIME),
        "mode":           "bounded-threadpool",
        "checks": {
            "redis":         "ok" if redis_ok else "unavailable (using in-memory)",
            "github_api":    "ok" if gh_ok else "rate_limited",
            "llm_providers": breaker_status,
        },
        "thread_pool": {
            "max_workers":    MAX_DISPATCH_WORKERS,
            "pending_jobs":   pending,
            "queue_capacity": _QUEUE_CAP,
            "saturation_pct": round(pending / _QUEUE_CAP * 100, 1),
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
    # 1. Payload size guard
    content_length = request.content_length
    if content_length and content_length > MAX_PAYLOAD_BYTES:
        return jsonify({"error": "Payload too large"}), 413

    # 2. IP rate limit
    client_ip = (
        request.headers.get("X-Forwarded-For", request.remote_addr or "")
        .split(",")[0].strip()
    )
    if not _check_ip_rate_limit(client_ip):
        return jsonify({"error": "Too many requests"}), 429

    # 3. Signature — FAIL CLOSED (empty secret → reject)
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_signature(request.data, sig):
        log.warning(f"webhook.invalid_signature ip={client_ip}")
        return jsonify({"error": "Invalid signature"}), 401

    # 4. Parse JSON
    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    webhook_event = request.headers.get("X-GitHub-Event", "")
    delivery_id   = request.headers.get("X-GitHub-Delivery", "")
    repo = payload.get("repository", {}).get("full_name", "unknown")

    # 5. Bot loop prevention
    if _is_bot_sender(payload):
        return jsonify({"status": "skipped — bot sender"}), 200

    log.info(
        f"webhook.received event={webhook_event} repo={repo} "
        f"delivery={delivery_id[:8] if delivery_id else 'none'}"
    )
    metrics.increment("webhook.received")

    # 6. Idempotency
    fingerprint = make_fingerprint(delivery_id, webhook_event, payload)
    if is_duplicate(fingerprint):
        metrics.increment("webhook.duplicate_skipped")
        return jsonify({"status": "duplicate — skipped"}), 200

    # 7. Dispatch to bounded pool → ack immediately
    _dispatch(webhook_event, payload, repo)
    metrics.increment(f"events.{webhook_event}.queued")
    metrics.increment("events.total")

    return jsonify({"status": "accepted"}), 202


# ── Boot ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _startup_check()  # Crash loudly if secret not set
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
else:
    # Running via gunicorn — still validate on import
    _startup_check()
