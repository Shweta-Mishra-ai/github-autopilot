"""
app/core/health_check.py
Per-provider latency and error-rate telemetry for /health, fed by the router
and the circuit breaker.
"""

import time
import logging

log = logging.getLogger(__name__)


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

    except Exception as e:
        log.debug(f"health_check.record_latency_failed provider={provider}: {e}")


def get_system_health() -> dict:
    """
    Per-provider breaker state, latency and error rate, for /health.

    This also used to report GitHub and Redis health and an overall status,
    and get_degraded_message() turned that into a comment banner. Nothing
    called get_degraded_message, and /health computes GitHub and Redis
    itself; the GitHub figure here defaulted to 5000 remaining when no call
    had been made, a health claim with no data behind it. Only the provider
    view, which /health does read, is kept.
    """
    from app.ai.circuit_breaker import status_all

    provider_health = {}
    for pk, cb_status in status_all().items():
        state = cb_status.get("state", "unknown")
        # None, not 0, when there are no samples or they cannot be read:
        # 0 ms and a 0% error rate are measurements nobody made.
        latency_stats = _get_latency_stats(pk) or {}
        provider_health[pk] = {
            "state": state,
            "is_degraded": state != "closed",
            "avg_latency_ms": latency_stats.get("avg"),
            "p95_latency_ms": latency_stats.get("p95"),
            "error_rate": latency_stats.get("err_rate"),
        }
    return {"providers": provider_health, "checked_at": int(time.time())}


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
            return {"avg": None, "p95": None, "err_rate": len(errors) / max(len(vals), 1)}

        latencies.sort()
        avg = sum(latencies) / len(latencies)
        p95_idx = int(len(latencies) * 0.95)
        p95 = latencies[min(p95_idx, len(latencies) - 1)]
        err_rate = len(errors) / max(len(vals), 1)

        return {"avg": round(avg), "p95": round(p95), "err_rate": round(err_rate, 3)}
    except Exception as e:
        log.warning(f"health_check.latency_unreadable provider={provider}: {e}")
        return {}
