"""
server.py — Flask entry point.

Security:  webhook_security.verify_webhook() — HMAC, replay, rate limit
Threading: thread_pool.dispatch()           — bounded pool, backpressure on saturation
Health:    /ping (public), /health (auth-gated detail)
"""

import hmac
import logging
import os
import signal
import time
import traceback

from flask import Flask, jsonify, request

from app import __version__
from app.core.metrics import metrics
from app.core.redis_client import is_redis_available
from app.core.thread_pool import is_saturated, pool_stats, shutdown
from app.core.webhook_security import startup_check
import app.core.idempotency as idempotency
import app.core.thread_pool as thread_pool
import app.core.webhook_security as webhook_security


def is_duplicate(*a, **kw):
    return idempotency.is_duplicate(*a, **kw)


def make_fingerprint(*a, **kw):
    return idempotency.make_fingerprint(*a, **kw)


def dispatch(*a, **kw):
    return thread_pool.dispatch(*a, **kw)


def verify_webhook(*a, **kw):
    return webhook_security.verify_webhook(*a, **kw)


logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("server")

app = Flask(__name__)

METRICS_TOKEN = os.environ.get("METRICS_AUTH_TOKEN", "")
START_TIME = time.time()
VERSION = __version__


def _authorized(req) -> bool:
    """
    Constant-time bearer check for /health and /metrics.

    Behaviour:
      - METRICS_TOKEN unset  → open (no secret to enforce). Logged once at boot.
      - METRICS_TOKEN set    → require exact `Authorization: Bearer <token>`,
                               compared in constant time to avoid timing leaks.
    """
    if not METRICS_TOKEN:
        return True
    auth = req.headers.get("Authorization", "")
    return hmac.compare_digest(auth, f"Bearer {METRICS_TOKEN}")


# ── Graceful shutdown ───────────────────────────────────────────────────────


def _handle_sigterm(signum, frame):
    log.info("server.sigterm_received — draining queue consumers + thread pool")
    from app.core.event_queue import stop_consumers

    stop_consumers()
    shutdown(wait=True)
    log.info("server.graceful_shutdown_complete")


signal.signal(signal.SIGTERM, _handle_sigterm)


# ── Routes ─────────────────────────────────────────────────────────────────


@app.route("/", methods=["GET"])
def index():
    return jsonify(
        {
            "app": "GitHub Autopilot",
            "version": VERSION,
            "status": "running",
            "docs": "https://github.com/Shweta-Mishra-ai/github-autopilot",
        }
    )


@app.route("/ping", methods=["GET"])
def ping():
    """Public liveness probe. Returns minimal response — no internal info."""
    return jsonify({"status": "ok", "version": VERSION}), 200


@app.route("/dashboard", methods=["GET"])
def dashboard():
    """
    Live ops dashboard (HTML). The shell contains no secret; it polls the
    auth-gated /health + /metrics from the browser using a token the operator
    pastes in (kept in sessionStorage, never in the URL).
    """
    from app.dashboard import dashboard_html

    return dashboard_html(), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/graph", methods=["GET"])
def graph():
    """
    Interactive codebase map (HTML). Like /dashboard, the shell holds no secret
    — it fetches /graph.json with a token the operator pastes in.
    """
    from app.graphview import graph_html

    return graph_html(), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/graph.json", methods=["GET"])
def graph_json():
    """
    The generated dependency graph.

    Auth-gated with the same token as /health: a dependency graph is a map of
    the codebase — module names, sizes, and what depends on what — and should
    not be public on a private deployment.

    Served from the file CI commits, not built per-request: walking and parsing
    every module on a web request would be slow and would report the *deployed*
    tree, which for an installed package is not the repository anyone is
    looking at.
    """
    if not _authorized(request):
        return jsonify({"error": "Unauthorized"}), 401

    path = os.environ.get("CODEGRAPH_PATH", "docs/diagrams/codegraph.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return app.response_class(fh.read(), mimetype="application/json")
    except FileNotFoundError:
        return jsonify(
            {
                "error": "No graph generated yet",
                "hint": (
                    "python -m app.intelligence.codegraph app server.py worker.py "
                    "--out docs/diagrams/codegraph.json"
                ),
            }
        ), 404
    except OSError as e:
        log.error(f"graph_json.read_failed path={path}: {e}")
        return jsonify({"error": "Could not read graph"}), 500


@app.route("/health", methods=["GET"])
def health():
    """
    Detailed health. Auth-gated when METRICS_AUTH_TOKEN is set.
    Use /ping for Render health checks (no auth needed).
    """
    if not _authorized(request):
        return jsonify({"error": "Unauthorized"}), 401

    from app.ai.circuit_breaker import status_all
    from app.core.redis_client import redis_memory_status
    from app.github.rate_limit import get_status as gh_rl_status

    redis_ok = is_redis_available()
    redis_mem = redis_memory_status()
    gh_ok = gh_rl_status().get("remaining", 5000) > 50
    breaker_status = status_all()
    any_llm_ok = any(s["state"] == "closed" for s in breaker_status.values())
    overall = "ok" if (gh_ok and any_llm_ok) else "degraded"

    pool = pool_stats()
    pool_saturated = pool.get("saturation_pct", 0) > 80

    return jsonify(
        {
            "status": overall,
            "version": VERSION,
            "uptime_seconds": int(time.time() - START_TIME),
            "checks": {
                "redis": "ok" if redis_ok else "unavailable",
                "redis_memory": redis_mem,
                "github_api": "ok" if gh_ok else "rate_limited",
                "llm_providers": breaker_status,
                "thread_pool": "saturated" if pool_saturated else "ok",
            },
            "thread_pool": pool,
            "event_queue": _queue_stats(),
            "metrics": {
                "events_total": metrics.get("events.total", 0),
                "errors_total": metrics.get("events.error", 0),
                "events_dropped": metrics.get("events.dropped", 0),
                "secondary_rate_limited": metrics.get("events.secondary_rate_limited", 0),
                # Non-zero means the collaborator-permission API is failing, so
                # every maintainer-only command is being denied regardless of who
                # runs it. Surfaced here because the symptom (commands "not
                # working") otherwise looks nothing like its cause.
                "permission_check_failures": metrics.get("auth.permission_check_failed", 0),
            },
            # Notifications are delivered on daemon threads, so a rejected
            # webhook only ever produced a log line nobody reads. "Slack went
            # quiet" is now answerable without grepping logs.
            "notifications": _notification_status(),
            # Per-provider latency and error rate. health_check computed these
            # from a dataset nothing wrote to, so they always read zero; the
            # router and the circuit breaker now feed it.
            "providers": _provider_health(),
        }
    ), 200 if overall == "ok" else 207


@app.route("/metrics", methods=["GET"])
def get_metrics():
    if not _authorized(request):
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify(metrics.snapshot())


@app.route("/mcp", methods=["POST"])
def mcp_endpoint():
    """MCP (Model Context Protocol) endpoint for IDE integrations."""
    from app.mcp.mcp_server import handle_mcp_request

    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""

    try:
        body = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"error": {"code": -32700, "message": "Parse error"}}), 400

    resp, status = handle_mcp_request(body.get("method", ""), body.get("params", {}), token)
    return jsonify(resp), status


@app.route("/mcp", methods=["GET"])
def mcp_info():
    """MCP server discovery endpoint."""
    # Derived, never hardcoded: a literal count silently lies the moment a
    # tool is added or removed, and this endpoint is public.
    from app.mcp.tools import MCP_TOOLS

    return jsonify(
        {
            "name": "github-autopilot",
            "version": VERSION,
            "protocol": "mcp/2024-11-05",
            "tools": len(MCP_TOOLS),
            "description": "AI-powered GitHub repository assistant",
            "auth": "Bearer token via MCP_API_KEY env var",
            "docs": "https://github.com/Shweta-Mishra-ai/github-autopilot/blob/main/docs/mcp-setup.md",
        }
    )


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
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
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

    # Durable path first: park event in Redis, consumers pick it up.
    # Survives restarts/deploys — thread-pool queue does not.
    from app.core.event_queue import EnqueueResult, enqueue

    eq = enqueue(webhook_event, payload, repo, delivery_id)

    if eq == EnqueueResult.OK:
        metrics.increment(f"events.{webhook_event}.queued")
        metrics.increment("events.total")
        return jsonify({"status": "queued"}), 202

    if eq == EnqueueResult.FULL:
        # Bounded queue full → 503 so GitHub redelivers later.
        metrics.increment("events.dropped")
        return jsonify({"error": "Server busy — please retry", "retry_after": 30}), 503

    # Redis down or envelope too large → degrade to direct thread-pool dispatch.
    result = _dispatch(webhook_event, payload, repo)

    # Saturated pool → 503 so GitHub retries automatically
    if is_saturated(result):
        metrics.increment("events.dropped")
        log.error(
            f"dispatch.saturated event={webhook_event} repo={repo} "
            "— returning 503 for GitHub retry"
        )
        return jsonify(
            {
                "error": "Server busy — please retry",
                "retry_after": 30,
            }
        ), 503

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
        from app.github.client import GitHubSecondaryRateLimitError

        if isinstance(e, GitHubSecondaryRateLimitError):
            log.warning(
                f"dispatch.secondary_rate_limit event={webhook_event} repo={repo} "
                f"retry_after={e.retry_after}s — dropping, GitHub will retry"
            )
            metrics.increment("events.secondary_rate_limited")
        else:
            log.error(f"dispatch.error event={webhook_event} repo={repo}: {e}")
            log.error(traceback.format_exc())
            metrics.increment(f"events.{webhook_event}.error")


def _dispatch(webhook_event: str, payload: dict, repo: str):
    """Returns Future on success, _SATURATED sentinel on queue full."""
    return dispatch(_run_handler, webhook_event, payload, repo)


def _queue_stats() -> dict:
    from app.core.event_queue import queue_stats

    return queue_stats()


def _provider_health() -> dict:
    """
    Per-provider latency and error rate from app/core/health_check.py.

    Degrades to an empty dict rather than failing /health — a telemetry gap
    must not take the health endpoint down with it.
    """
    try:
        from app.core.health_check import get_system_health

        return get_system_health().get("providers", {})
    except Exception as e:
        log.debug(f"health.provider_stats_unavailable: {e}")
        return {}


def _notification_status() -> dict:
    """
    Whether each channel is configured, and how its deliveries have gone.

    `configured: false` means no webhook URL is set for that channel — the most
    common reason notifications "stop working", and previously invisible.
    """
    from app.github.notifications import discord_enabled, slack_enabled

    out = {}
    for channel, configured in (
        ("slack", slack_enabled()),
        ("discord", discord_enabled()),
    ):
        sent = metrics.get(f"notifications.{channel}.sent", 0)
        failed = metrics.get(f"notifications.{channel}.failed", 0)
        out[channel] = {
            "configured": configured,
            "sent": sent,
            "failed": failed,
            "status": "ok" if configured and not failed else ("failing" if failed else "off"),
        }
    return out


def _boot():
    """Shared boot path for gunicorn import and `python server.py`."""
    startup_check()
    from app.core.event_queue import start_consumers

    start_consumers(_run_handler)


# ── Boot ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _boot()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
else:
    # Running via gunicorn — validate credentials + start queue consumers on import
    _boot()
