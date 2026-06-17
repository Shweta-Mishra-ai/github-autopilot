"""
server.py — Flask entry point.

Security:  webhook_security.verify_webhook() — HMAC, replay, rate limit
Threading: thread_pool.dispatch()           — bounded pool, queue cap
Health:    /ping (public), /health (auth-gated detail)
"""

import logging
import os
import time
import traceback

from flask import Flask, jsonify, request

from app.core.idempotency  import is_duplicate, make_fingerprint
from app.core.metrics      import metrics
from app.core.redis_client import is_redis_available
from app.core.thread_pool  import dispatch, pool_stats
from app.core.webhook_security import verify_webhook, startup_check

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("server")

app = Flask(__name__)

METRICS_TOKEN = os.environ.get("METRICS_AUTH_TOKEN", "")
START_TIME    = time.time()
VERSION       = "4.2.0"


# ── Routes ─────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "app":     "AI Repo Manager",
        "version": VERSION,
        "status":  "running",
        "docs":    "https://github.com/Shweta-Mishra-ai/github-autopilot",
    })


@app.route("/ping", methods=["GET"])
def ping():
    """Public liveness probe. Returns minimal response — no internal info."""
    return jsonify({"status": "ok", "version": VERSION}), 200


@app.route("/health", methods=["GET"])
def health():
    """
    Detailed health. Auth-gated when METRICS_AUTH_TOKEN is set.
    Use /ping for Render health checks (no auth needed).
    """
    if METRICS_TOKEN:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {METRICS_TOKEN}":
            return jsonify({"error": "Unauthorized"}), 401

    from app.ai.circuit_breaker import status_all
    from app.github.rate_limit  import get_status as gh_rl_status

    redis_ok       = is_redis_available()
    gh_ok          = gh_rl_status().get("remaining", 5000) > 50
    breaker_status = status_all()
    any_llm_ok     = any(s["state"] == "closed" for s in breaker_status.values())
    overall        = "ok" if (gh_ok and any_llm_ok) else "degraded"

    return jsonify({
        "status":         overall,
        "version":        VERSION,
        "uptime_seconds": int(time.time() - START_TIME),
        "checks": {
            "redis":         "ok" if redis_ok else "unavailable",
            "github_api":    "ok" if gh_ok    else "rate_limited",
            "llm_providers": breaker_status,
        },
        "thread_pool": pool_stats(),
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




@app.route("/mcp", methods=["POST"])
def mcp_endpoint():
    """MCP (Model Context Protocol) endpoint for IDE integrations."""
    from app.mcp.server import handle_mcp_request

    auth  = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""

    try:
        body = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"error": {"code": -32700, "message": "Parse error"}}), 400

    resp, status = handle_mcp_request(
        body.get("method", ""), body.get("params", {}), token
    )
    return jsonify(resp), status


@app.route("/mcp", methods=["GET"])
def mcp_info():
    """MCP server discovery endpoint."""
    return jsonify({
        "name":        "github-autopilot",
        "version":     VERSION,
        "protocol":    "mcp/2024-11-05",
        "tools":       8,
        "description": "AI-powered GitHub repository assistant",
        "auth":        "Bearer token via MCP_API_KEY env var",
        "docs":        "https://github.com/Shweta-Mishra-ai/github-autopilot/blob/main/docs/mcp-setup.md",
    })

@app.route("/webhook", methods=["POST"])
def webhook():
    # Full security pipeline: size + IP rate limit + HMAC + replay
    ok, err = verify_webhook(request)
    if not ok:
        status = 429 if "Too many" in err else 413 if "large" in err else 401
        log.warning(f"webhook.rejected reason={err!r}")
        return jsonify({"error": err}), status

    # Parse JSON
    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    webhook_event = request.headers.get("X-GitHub-Event", "")
    delivery_id   = request.headers.get("X-GitHub-Delivery", "")
    repo = payload.get("repository", {}).get("full_name", "unknown")

    # Bot loop prevention
    from app.core.webhook_security import is_bot_sender
    if is_bot_sender(payload):
        return jsonify({"status": "skipped — bot sender"}), 200

    log.info(
        f"webhook.received event={webhook_event} repo={repo} "
        f"delivery={delivery_id[:8] if delivery_id else 'none'}"
    )
    metrics.increment("webhook.received")

    # Idempotency — deduplicate retries
    fingerprint = make_fingerprint(delivery_id, webhook_event, payload)
    if is_duplicate(fingerprint):
        metrics.increment("webhook.duplicate_skipped")
        return jsonify({"status": "duplicate — skipped"}), 200

    # Dispatch to bounded pool — ACK immediately
    _dispatch(webhook_event, payload, repo)
    metrics.increment(f"events.{webhook_event}.queued")
    metrics.increment("events.total")

    return jsonify({"status": "accepted"}), 202


# ── Dispatch ───────────────────────────────────────────────────────────────

def _run_handler(webhook_event: str, payload: dict, repo: str):
    """Runs inside the thread pool. All errors caught — never crashes pool."""
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
                log.debug("ci handler not available — skipping")

        else:
            log.debug(f"dispatch.unhandled event={webhook_event}")
            return

        metrics.increment(f"events.{webhook_event}.success")
        log.info(f"dispatch.done event={webhook_event}")

    except Exception as e:
        log.error(f"dispatch.error event={webhook_event} repo={repo}: {e}")
        log.error(traceback.format_exc())
        metrics.increment(f"events.{webhook_event}.error")


def _dispatch(webhook_event: str, payload: dict, repo: str):
    result = dispatch(_run_handler, webhook_event, payload, repo)
    if result is None:
        log.error(
            f"dispatch.dropped event={webhook_event} repo={repo} "
            "— pool saturated"
        )
        metrics.increment("events.dropped")


# ── Boot ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    startup_check()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
else:
    # Running via gunicorn — validate on import
    startup_check()
