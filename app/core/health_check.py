"""
app/core/health_check.py
V4 Sprint 5: Smart health check with degraded mode.

PROBLEM:
  When Groq is slow, bot silently times out.
  User thinks bot is broken — no feedback.

SOLUTION:
  Degraded mode: bot posts "⚠️ Degraded" comment when provider is slow.
  Health check tracks response times per provider.
  /health endpoint shows real-time degraded status.
"""

import time
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# SLA thresholds (seconds)
SLA_FAST = 3.0  # fast tasks should complete in 3s
SLA_NORMAL = 8.0  # normal tasks in 8s
SLA_SLOW = 20.0  # anything > 20s is degraded


@dataclass
class ProviderHealth:
    provider: str
    avg_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    error_rate: float = 0.0
    is_degraded: bool = False
    last_updated: float = field(default_factory=time.time)


def record_latency(provider: str, latency_ms: int, is_error: bool = False):
    """
    Record a provider call. Used by router._log_and_track().
    Stores rolling window in Redis.
    """
    try:
        from app.core.redis_client import get_redis

        r = get_redis()
        key = f"health:latency:{provider}"

        # Store last 50 latencies as JSON list
        import json

        raw = r.get(key)
        vals = json.loads(raw) if raw else []
        vals.append({"ms": latency_ms, "err": is_error, "t": int(time.time())})
        vals = vals[-50:]  # keep last 50
        r.set(key, json.dumps(vals), ex=3600)

    except Exception:
        pass


def get_system_health() -> dict:
    """
    Returns complete system health for /health endpoint.
    Includes: LLM providers, GitHub API, Redis, overall status.
    """
    from app.ai.circuit_breaker import status_all
    from app.github.rate_limit import get_status as gh_rate

    breakers = status_all()
    gh_status = gh_rate()
    redis_ok = _check_redis()

    provider_health = {}
    any_degraded = False

    for pk, cb_status in breakers.items():
        state = cb_status.get("state", "unknown")
        degraded = state != "closed"
        if degraded:
            any_degraded = True

        latency_stats = _get_latency_stats(pk)
        provider_health[pk] = {
            "state": state,
            "is_degraded": degraded,
            "avg_latency_ms": latency_stats.get("avg", 0),
            "p95_latency_ms": latency_stats.get("p95", 0),
            "error_rate": latency_stats.get("err_rate", 0.0),
        }

    # GitHub API health
    gh_ok = gh_status.get("remaining", 5000) > 100
    if not gh_ok:
        any_degraded = True

    # Overall status
    if not redis_ok or not gh_ok:
        overall = "degraded"
    elif any_degraded:
        overall = "partial"
    else:
        overall = "ok"

    return {
        "status": overall,
        "is_degraded": overall != "ok",
        "providers": provider_health,
        "github_api": {
            "remaining": gh_status.get("remaining", 0),
            "resets_in": gh_status.get("resets_in", 0),
            "is_healthy": gh_ok,
        },
        "redis": {
            "connected": redis_ok,
        },
        "checked_at": int(time.time()),
    }


def get_degraded_message() -> str:
    """
    Returns a user-visible degraded status message for GitHub comments.
    Called when bot detects it's running in degraded mode.
    """
    health = get_system_health()
    if not health["is_degraded"]:
        return ""

    degraded_providers = [
        pk for pk, ph in health["providers"].items() if ph["is_degraded"]
    ]

    if not health["github_api"]["is_healthy"]:
        remaining = health["github_api"]["remaining"]
        resets = health["github_api"]["resets_in"]
        return (
            f"\n\n> ⚠️ **GitHub API rate limit low** ({remaining} requests remaining, "
            f"resets in {resets}s). Some bot features may be slower."
        )

    if degraded_providers:
        providers_str = ", ".join(f"`{p}`" for p in degraded_providers)
        return (
            f"\n\n> ⚠️ **AI provider degraded**: {providers_str}. "
            "Response quality may be reduced. Retrying automatically."
        )

    return ""


def _check_redis() -> bool:
    try:
        from app.core.redis_client import is_redis_available

        return is_redis_available()
    except Exception:
        return False


def _get_latency_stats(provider: str) -> dict:
    try:
        import json
        from app.core.redis_client import get_redis

        r = get_redis()
        raw = r.get(f"health:latency:{provider}")
        if not raw:
            return {}
        vals = json.loads(raw)
        latencies = [v["ms"] for v in vals if not v.get("err")]
        errors = [v for v in vals if v.get("err")]

        if not latencies:
            return {"avg": 0, "p95": 0, "err_rate": len(errors) / max(len(vals), 1)}

        latencies.sort()
        avg = sum(latencies) / len(latencies)
        p95_idx = int(len(latencies) * 0.95)
        p95 = latencies[min(p95_idx, len(latencies) - 1)]
        err_rate = len(errors) / max(len(vals), 1)

        return {"avg": round(avg), "p95": round(p95), "err_rate": round(err_rate, 3)}
    except Exception:
        return {}
