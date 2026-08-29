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
from app.core.webhook_security import MAX_PAYLOAD_BYTES, startup_check
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

# Refuse an oversized body while it is being READ, not after.
#
# verify_webhook() checks len(request.data), which cannot run until the entire
# body has been materialised — measured at 62 MB of peak allocation to reject a
# 30 MB request, and the size check is step one, so no signature is required to
# trigger it. A handful of concurrent requests exhausts a 512 MB instance.
#
# Werkzeug enforces this limit against the stream and raises
# RequestEntityTooLarge before the body reaches the application. The explicit
# length check in verify_webhook stays as defence in depth for a chunked
# request that declares no Content-Length.
app.config["MAX_CONTENT_LENGTH"] = MAX_PAYLOAD_BYTES

METRICS_TOKEN = os.environ.get("METRICS_AUTH_TOKEN", "")
START_TIME = time.time()
VERSION = __version__


@app.errorhandler(413)
def _payload_too_large(_e):
    """Werkzeug aborts oversized bodies before any route runs; answer in the
    same JSON shape every other rejection uses so a client does not have to
    parse an HTML error page."""
    metrics.increment("webhook.rejected_too_large")
    return jsonify({"error": "Payload too large"}), 413


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


def _base_url() -> str:
    """Where GitHub should call back. PUBLIC_URL wins; else the request's host."""
    from app.setup_flow import _public_url

    return _public_url() or request.url_root.rstrip("/")


@app.route("/setup", methods=["GET"])
def setup():
    """
    One-click GitHub App creation.

    Deliberately NOT auth-gated: it is the page you reach before you have any
    credentials to authenticate with. It exposes nothing — the manifest is the
    same permission list published in the README, and the only secret in this
    flow appears on the callback, which requires a code GitHub issues.
    """
    from app.setup_flow import setup_page

    return setup_page(_base_url()), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/setup/callback", methods=["GET"])
def setup_callback():
    """Exchange GitHub's one-time code for the App credentials, and show them once."""
    from app.setup_flow import consume_state, credentials_page, exchange_code

    code = (request.args.get("code") or "").strip()
    state = (request.args.get("state") or "").strip()

    if not code:
        return (
            "<h1>Missing code</h1><p>Start again at <a href='/setup'>/setup</a>.</p>",
            400,
            {"Content-Type": "text/html; charset=utf-8"},
        )
    if not consume_state(state):
        # A callback this server did not initiate, or one already used.
        log.warning("setup.callback_state_rejected")
        return (
            "<h1>This link is no longer valid</h1><p>Setup links are single-use. "
            "Start again at <a href='/setup'>/setup</a>.</p>",
            400,
            {"Content-Type": "text/html; charset=utf-8"},
        )

    data, error = exchange_code(code)
    if error:
        return (
            f"<h1>Setup could not complete</h1><p>{error}</p>"
            "<p><a href='/setup'>Start again</a></p>",
            502,
            {"Content-Type": "text/html; charset=utf-8"},
        )

    # Deliberately no logging of `data` — it carries the private key.
    log.info("setup.app_created app_id=%s", data.get("id"))
    return (
        credentials_page(data, _base_url()),
        200,
        {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"},
    )


@app.route("/setup/doctor", methods=["GET"])
def setup_doctor():
    """
    Why a command is not working on a given repository, with evidence.

    Auth-gated like /health: it names repositories and reports what the App is
    permitted to do, which is not public information.

        /setup/doctor?repo=owner/name&installation_id=123

    Probes are all GETs, so calling this can change nothing.
    """
    if not _authorized(request):
        return jsonify({"error": "Unauthorized"}), 401

    from app.core.preflight import (
        diagnose,
        format_environment_report,
        format_report,
        inspect_environment,
    )

    repo = (request.args.get("repo") or "").strip()
    raw_id = (request.args.get("installation_id") or "").strip()

    def _env_payload():
        findings = inspect_environment()
        return [{"name": f.name, "state": f.state, "detail": f.detail} for f in findings], findings

    def _models_payload():
        """
        Whether each configured model id is one the provider still serves.

        Answered HERE rather than in CI because the answer needs a key, and
        the key is a deployment secret. CI could not check Gemini for exactly
        that reason. The running service holds the key, so it can just ask.

        Never raises: a diagnostic that dies while diagnosing is worse than
        one that says "unknown".
        """
        try:
            from app.ai.router import format_model_configuration, model_configuration_report

            report = model_configuration_report()
            return report, format_model_configuration(report)
        except Exception as exc:
            log.debug(f"doctor.model_report_failed: {exc}")
            return {"error": type(exc).__name__}, ""

    # The deployment settings need neither a repo nor a token, and they are
    # most useful precisely when those are wrong. So answer with them rather
    # than refusing outright — a diagnostic that requires you to already have
    # the working configuration is not much of a diagnostic.
    if not repo or "/" not in repo or not raw_id.isdigit():
        env_json, findings = _env_payload()
        models_json, models_md = _models_payload()
        return jsonify(
            {
                "error": "repo and installation_id are required to probe permissions",
                "usage": "/setup/doctor?repo=owner/name&installation_id=123",
                "hint": (
                    "The installation id is in the URL of the App's installation "
                    "settings page, and in the `installation.id` field of any "
                    "webhook delivery."
                ),
                "environment": env_json,
                "models": models_json,
                "report_markdown": format_environment_report(findings)
                + ("\n\n" + models_md if models_md else ""),
            }
        ), 400

    result = diagnose(repo, int(raw_id), actor=(request.args.get("actor") or "").strip())
    # Once: each call reads three provider catalogues over the network.
    models_json, models_md = _models_payload()
    payload = {
        "repo": result.repo,
        "installation_id": result.installation_id,
        "healthy": result.healthy,
        "error": result.error,
        "granted_permissions": result.granted,
        "probes": [
            {
                "capability": p.capability,
                "ok": p.ok,
                "status": p.status,
                "detail": p.detail,
                "enables": list(p.enables),
                "required": p.required,
            }
            for p in result.probes
        ],
        "environment": _env_payload()[0],
        "models": models_json,
        "report_markdown": format_report(result) + ("\n\n" + models_md if models_md else ""),
    }
    return jsonify(payload), 200 if result.healthy else 207


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

    # A provider misconfiguration is worse than a degraded provider: it cannot
    # recover on its own and every AI command fails until someone changes an
    # environment variable. Open breakers alone cannot express that — they look
    # identical to a bad hour at the provider, which is worth waiting out.
    from app.ai.router import LLMRouter

    llm_config_error = ""
    llm_models = {}
    llm_substitutions = {}
    try:
        ai_status = LLMRouter().status()
        llm_config_error = ai_status.get("configuration_error", "")
        llm_models = ai_status.get("models", {})

        # A model swapped in because the configured one was retired. The bot
        # keeps working, which is the point -- but a bot answering on a model
        # nobody chose is its own incident, so it is never silent.
        from app.ai.model_catalog import active_substitutions

        llm_substitutions = {k: v.get("to", "") for k, v in active_substitutions().items()}
    except Exception as exc:  # health must not fail on its own reporting
        log.debug(f"health.llm_status_failed: {exc}")

    overall = "ok" if (gh_ok and any_llm_ok and not llm_config_error) else "degraded"
    if llm_substitutions:
        # Answering, but on a model nobody configured. Not "ok", not an outage.
        overall = "degraded" if overall == "ok" else overall
    if llm_config_error:
        overall = "misconfigured"

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
                "llm_models": llm_models,
                # Non-empty means the provider is answering, and refusing:
                # a model id it does not serve, or a key it will not accept.
                # Retrying cannot fix either, so this is reported separately
                # from the breakers rather than folded into "degraded".
                "llm_configuration_error": llm_config_error,
                # Empty in a healthy deployment. Non-empty means the configured
                # model id is gone and one from the provider's own catalogue is
                # being used instead -- working, but not what was asked for.
                "llm_model_substitutions": llm_substitutions,
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
            # The 15-day sweep and the encrypted memory backup. A schedule that
            # silently stopped running looks exactly like one that is running
            # fine, so next_run_at and the last result are reported rather than
            # a bare on/off — an operator can see it is due, overdue, or never
            # configured.
            "maintenance": _maintenance_status(),
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

    # `[]`, `"str"` and `123` are all valid JSON and none of them has .get().
    # Every line below assumes a mapping, so the AttributeError surfaced as a
    # 500 — an internal error for what is really a malformed request.
    if not isinstance(payload, dict):
        log.warning(f"webhook.non_object_payload type={type(payload).__name__}")
        return jsonify({"error": "Payload must be a JSON object"}), 400

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

        # Record which installation can act on this repo. The id arrives only
        # on the webhook, and nothing persisted it — so anything that runs on a
        # schedule rather than in response to an event (the 15-day security
        # sweep) had no credential for any repository at all.
        try:
            from app.core.installations import remember_installation, touch

            inst = (payload.get("installation") or {}).get("id", 0)
            if repo and inst:
                remember_installation(repo, inst)
                touch(repo)
        except Exception as e:
            log.debug(f"installations.record_skipped repo={repo}: {e}")

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


def _maintenance_status() -> dict:
    """
    Scheduled-sweep and backup state. Never raises — /health must answer even
    when the thing it is reporting on is broken.

    `overdue` is the value worth reading: the scheduler is a daemon thread on a
    free tier that restarts often, and the due time lives in Redis precisely so
    a restart cannot silently reset the clock. If this is ever true, the pass
    is not running and nobody would otherwise find out for 15 days.
    """
    import time as _time

    try:
        from app.core.maintenance import status as maintenance_state
        from app.core.memory_backup import backup_status

        state = maintenance_state()
        due = state.get("next_run_at") or 0
        state["overdue"] = bool(due and _time.time() > due + 3600)
        state["memory_backup"] = backup_status()
        return state
    except Exception as e:
        return {"error": str(e)[:120]}


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
    _boot_maintenance()


def _boot_maintenance():
    """
    Restore memory if it is empty, then start the periodic maintenance pass.

    Both are best-effort: a backup that cannot be reached must not stop the app
    from serving webhooks, which is its actual job. Ordering matters — restore
    runs before the scheduler so a wiped instance is warm before anything reads
    memory, and it is safe to run in every worker because it no-ops unless
    memory is genuinely empty.
    """
    try:
        from app.core.memory_backup import maybe_restore_on_boot

        maybe_restore_on_boot()
    except Exception as e:
        log.warning(f"boot.memory_restore_skipped: {e}")

    try:
        from app.core.maintenance import start_scheduler

        start_scheduler()
    except Exception as e:
        log.warning(f"boot.maintenance_not_started: {e}")


# ── Boot ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _boot()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
else:
    # Running via gunicorn — validate credentials + start queue consumers on import
    _boot()
