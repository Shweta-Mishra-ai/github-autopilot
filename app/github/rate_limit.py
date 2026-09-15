"""
app/github/rate_limit.py — GitHub API rate-limit tracking.

Two properties this module has to get right, and got wrong:

1. **A rate limit belongs to one installation, not to the deployment.**
   GitHub meters per token. This module kept a single global `remaining`, and
   every response from every installation overwrote it. On a multi-tenant
   deployment that is not an approximation, it is the wrong number: one busy
   installation made every other one look exhausted, one idle installation
   masked a real exhaustion, and `/health` reported a figure that belonged to
   nobody. State is now keyed per token.

2. **Nothing here may sleep in a shared worker thread.**
   `check_and_wait()` slept for up to two minutes, and it is called at the top
   of every request in app/github/client.py. Handlers run on a bounded pool
   (MAX_DISPATCH_WORKERS, 6 by default), so six throttled calls stalled every
   webhook for the duration and the queue shed the rest as 503s.

   That is the same defect app/github/client.py fixed for the *secondary*
   rate limit, where the comment reads "Never sleep in a shared worker
   thread" — three lines below the call to this function, which did.

   The rule now holds here too: a short delay is worth riding out, a long one
   is reported. app/ai/router.py draws the same line for provider throttling,
   for the same reason.

Tokens are never stored or logged. State is keyed by a truncated SHA-256 of
the token, which is stable for the life of a token and reveals nothing.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

# Below this many remaining requests, calls are held or refused.
SAFETY_BUFFER = 50

# The longest this will ever block a worker thread. A primary rate limit
# resets on the hour, so the honest answer to a long wait is to refuse and let
# GitHub redeliver the webhook — the queue is built for exactly that. Waiting
# is only ever worth it for the last few seconds of a window.
DEFAULT_MAX_WAIT_SECONDS = 10

# Installations tracked at once. A bound is needed because the key is derived
# from the token, and a token rotates; without this, a long-lived process on a
# many-tenant deployment would accumulate an entry per rotation.
MAX_TRACKED = 256

_GLOBAL_KEY = "unknown"

_state: dict[str, dict] = {}
_lock = threading.Lock()


def _max_wait_seconds() -> float:
    """Read at call time, not import time, so it can be changed per deployment."""
    raw = os.environ.get("GITHUB_MAX_RATE_LIMIT_WAIT_SECONDS", "")
    try:
        value = float(raw) if raw else DEFAULT_MAX_WAIT_SECONDS
    except (TypeError, ValueError):
        log.warning(f"rate_limit.bad_max_wait value={raw!r} — using default")
        return float(DEFAULT_MAX_WAIT_SECONDS)
    return max(0.0, value)


def token_key(token: str | None) -> str:
    """
    Stable, non-reversible identity for a token.

    Not the token, and not derived from anything a log line would leak: the
    whole point is that per-installation state can be kept without the state
    store ever holding a credential.
    """
    if not token:
        return _GLOBAL_KEY
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _blank(key: str) -> dict:
    return {
        "key": key,
        "remaining": 5000,
        "reset_at": 0,
        "last_checked": 0.0,
        "resource": "core",
    }


def _evict_if_needed() -> None:
    """Drop the least recently seen entries. Caller holds the lock."""
    if len(_state) <= MAX_TRACKED:
        return
    oldest = sorted(_state.values(), key=lambda s: s.get("last_checked", 0.0))
    for entry in oldest[: len(_state) - MAX_TRACKED]:
        _state.pop(entry["key"], None)


def update_from_headers(headers: dict, token: str | None = None) -> None:
    """
    Record the rate-limit headers GitHub returns on every response.

    `token` identifies whose limit this is. It is optional so that any caller
    written against the old single-bucket signature keeps working; those
    responses land in a shared bucket rather than corrupting a real one.
    """
    try:
        key = token_key(token)
        remaining = headers.get("X-RateLimit-Remaining")
        reset_at = headers.get("X-RateLimit-Reset")
        resource = headers.get("X-RateLimit-Resource", "core")

        with _lock:
            entry = _state.setdefault(key, _blank(key))
            if remaining is not None:
                entry["remaining"] = int(remaining)
            if reset_at is not None:
                entry["reset_at"] = int(reset_at)
            entry["last_checked"] = time.time()
            entry["resource"] = resource
            _evict_if_needed()

        if remaining is not None:
            _try_redis_set(f"gh_rl_{resource}_{key}_remaining", remaining)
        if reset_at is not None:
            _try_redis_set(f"gh_rl_{resource}_{key}_reset", reset_at)

    except Exception as e:
        log.debug(f"rate_limit.update_from_headers_failed: {e}")


def check_and_wait(token: str | None = None) -> None:
    """
    Hold briefly, or refuse, when this installation's budget is nearly spent.

    Never blocks longer than `GITHUB_MAX_RATE_LIMIT_WAIT_SECONDS` (default 10).
    Beyond that it raises, because the caller is a pooled worker thread and the
    webhook that triggered it will be redelivered by GitHub — losing one
    request is strictly better than freezing every other one behind it.

    Raises RuntimeError, matching the previous contract so existing error
    handling is unaffected.
    """
    key = token_key(token)
    with _lock:
        entry = _state.get(key) or _state.get(_GLOBAL_KEY)
        remaining = entry["remaining"] if entry else 5000
        reset_at = entry["reset_at"] if entry else 0

    if remaining >= SAFETY_BUFFER:
        return

    wait_seconds = max(0.0, reset_at - time.time())
    limit = _max_wait_seconds()

    if wait_seconds <= limit:
        if wait_seconds > 0:
            log.warning(
                f"github.rate_limit_low remaining={remaining} "
                f"— holding {wait_seconds:.1f}s for reset"
            )
            time.sleep(wait_seconds)
        return

    log.error(
        f"github.rate_limit_exhausted remaining={remaining} "
        f"resets_in={wait_seconds:.0f}s — refusing rather than holding a worker"
    )
    raise RuntimeError(
        f"GitHub rate limit exhausted ({remaining} remaining). " f"Resets in {wait_seconds:.0f}s."
    )


def get_status(token: str | None = None) -> dict:
    """
    Rate-limit status, in the shape `/health` and health_check.py already read.

    With no token it reports the installation in the worst shape, which is the
    right answer to "can this deployment still call GitHub" — an average would
    let one idle installation hide another that is exhausted. `installations`
    carries the per-token breakdown for anyone who needs it.
    """
    now = time.time()
    with _lock:
        entries = [dict(e) for e in _state.values()]
        specific = dict(_state.get(token_key(token), {})) if token else {}

    if specific:
        worst = specific
    elif entries:
        worst = min(entries, key=lambda e: e.get("remaining", 5000))
    else:
        worst = _blank(_GLOBAL_KEY)

    reset_at = worst.get("reset_at", 0)
    return {
        "remaining": worst.get("remaining", 5000),
        "reset_at": reset_at,
        "resets_in": max(0, int(reset_at - now)) if reset_at else 0,
        "low": worst.get("remaining", 5000) < SAFETY_BUFFER,
        "resource": worst.get("resource", "core"),
        # Empty until a call has been made. Never contains a token.
        "installations": {
            e["key"]: {
                "remaining": e.get("remaining", 5000),
                "resets_in": max(0, int(e.get("reset_at", 0) - now)) if e.get("reset_at") else 0,
            }
            for e in entries
        },
    }


def fetch_live_status(token: str) -> dict:
    """
    Ask GitHub directly. Updates this token's tracked state as a side effect.
    Returns {} on any failure — a diagnostic must not raise.
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
            key = token_key(token)
            with _lock:
                entry = _state.setdefault(key, _blank(key))
                entry["remaining"] = core.get("remaining", entry["remaining"])
                entry["reset_at"] = core.get("reset", entry["reset_at"])
                entry["last_checked"] = time.time()
                _evict_if_needed()
            return {
                "core": core,
                "search": data.get("resources", {}).get("search", {}),
                "graphql": data.get("resources", {}).get("graphql", {}),
            }
    except Exception as e:
        log.debug(f"rate_limit.fetch_failed: {e}")
    return {}


def reset_state() -> None:
    """Drop all tracked state. For tests and for a re-installed App."""
    with _lock:
        _state.clear()


def _try_redis_set(key: str, value, ttl: int = 3600):
    """Share state across processes. Best effort — never affects the request."""
    try:
        from app.core.redis_client import get_redis

        get_redis().set(f"github:{key}", str(value), ex=ttl)
    except Exception as e:
        log.debug(f"rate_limit.redis_set_failed key={key}: {e}")
