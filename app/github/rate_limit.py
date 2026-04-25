"""
app/github/rate_limit.py
V4 Sprint 4: GitHub API rate limit tracker.

UPDATED: Added live rate limit fetch from GitHub API.
UPDATED: Redis-backed state so multiple workers share rate limit info.
UPDATED: Per-resource tracking (core, search, graphql).
"""

import time
import logging

log = logging.getLogger(__name__)

# In-memory fallback state
_state = {
    "remaining": 5000,
    "reset_at": 0,
    "last_checked": 0,
    "resource": "core",
}

SAFETY_BUFFER = 50  # Don't call API when below this


def update_from_headers(headers: dict):
    """
    Call after every GitHub API response to track rate limits.
    GitHub returns these headers on every response.
    """
    try:
        remaining = headers.get("X-RateLimit-Remaining")
        reset_at = headers.get("X-RateLimit-Reset")
        resource = headers.get("X-RateLimit-Resource", "core")

        if remaining is not None:
            _state["remaining"] = int(remaining)
            _try_redis_set(f"gh_rl_{resource}_remaining", remaining)

        if reset_at is not None:
            _state["reset_at"] = int(reset_at)
            _try_redis_set(f"gh_rl_{resource}_reset", reset_at)

        # Always update these regardless
        _state["last_checked"] = time.time()
        _state["resource"] = resource  # track which resource was last checked

    except Exception:
        pass


def check_and_wait():
    """
    If rate limit is critically low — wait until reset.
    Call before important GitHub API calls.
    Raises RuntimeError if wait > 2 minutes (not worth waiting).
    """
    remaining = _state.get("remaining", 5000)
    reset_at = _state.get("reset_at", 0)

    if remaining < SAFETY_BUFFER:
        wait_seconds = max(0, reset_at - time.time()) + 5
        if 0 < wait_seconds < 120:
            log.warning(
                f"GitHub rate limit low ({remaining} remaining) "
                f"— waiting {wait_seconds:.0f}s"
            )
            time.sleep(wait_seconds)
        elif wait_seconds >= 120:
            log.error(
                f"GitHub rate limit exhausted. "
                f"Reset in {wait_seconds:.0f}s — skipping action."
            )
            raise RuntimeError(
                f"GitHub rate limit exhausted. Resets in {wait_seconds:.0f}s."
            )


def get_status() -> dict:
    """Returns current rate limit status. Used by /health endpoint."""
    remaining = _state["remaining"]
    reset_at = _state["reset_at"]
    now = time.time()
    resets_in = max(0, int(reset_at - now)) if reset_at else 0

    return {
        "remaining": remaining,
        "reset_at": reset_at,
        "resets_in": resets_in,
        "low": remaining < SAFETY_BUFFER,
        "resource": _state.get("resource", "core"),
    }


def fetch_live_status(token: str) -> dict:
    """
    Fetch current rate limit from GitHub API directly.
    Updates internal state. Used by /health endpoint.
    """
    try:
        import requests

        resp = requests.get(
            "https://api.github.com/rate_limit",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3+json",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            core = data.get("resources", {}).get("core", {})
            _state["remaining"] = core.get("remaining", _state["remaining"])
            _state["reset_at"] = core.get("reset", _state["reset_at"])
            _state["last_checked"] = time.time()
            return {
                "core": data.get("resources", {}).get("core", {}),
                "search": data.get("resources", {}).get("search", {}),
                "graphql": data.get("resources", {}).get("graphql", {}),
            }
    except Exception as e:
        log.debug(f"rate_limit.fetch_failed: {e}")
    return {}


def _try_redis_set(key: str, value, ttl: int = 3600):
    """Store rate limit state in Redis so all workers share it."""
    try:
        from app.core.redis_client import get_redis

        r = get_redis()
        r.set(f"github:{key}", str(value), ex=ttl)
    except Exception:
        pass
