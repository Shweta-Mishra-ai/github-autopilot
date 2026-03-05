"""
Rate Limit Tracker - app/github/rate_limit.py
Tracks GitHub API rate limits and backs off intelligently.
"""

import time
import logging

log = logging.getLogger(__name__)

_state = {
    "remaining": 5000,
    "reset_at": 0,
    "last_checked": 0,
}

# Don't make API calls when remaining is below this threshold
SAFETY_BUFFER = 50


def update_from_headers(headers: dict):
    """Call this after every GitHub API response to track rate limits."""
    try:
        remaining = headers.get("X-RateLimit-Remaining")
        reset_at = headers.get("X-RateLimit-Reset")
        if remaining is not None:
            _state["remaining"] = int(remaining)
        if reset_at is not None:
            _state["reset_at"] = int(reset_at)
        _state["last_checked"] = time.time()
    except Exception:
        pass


def check_and_wait():
    """
    If rate limit is critically low — wait until reset.
    Called before important GitHub API calls.
    """
    remaining = _state.get("remaining", 5000)
    reset_at = _state.get("reset_at", 0)

    if remaining < SAFETY_BUFFER:
        wait_seconds = max(0, reset_at - time.time()) + 5
        if wait_seconds > 0 and wait_seconds < 120:
            log.warning(f"GitHub rate limit low ({remaining} remaining) — waiting {wait_seconds:.0f}s")
            time.sleep(wait_seconds)
        elif wait_seconds >= 120:
            log.error(f"GitHub rate limit exhausted. Reset in {wait_seconds:.0f}s — skipping action.")
            raise RuntimeError(f"GitHub rate limit exhausted. Resets in {wait_seconds:.0f}s.")


def get_status() -> dict:
    return {
        "remaining": _state["remaining"],
        "reset_at": _state["reset_at"],
        "low": _state["remaining"] < SAFETY_BUFFER,
    }
