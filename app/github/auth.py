"""
GitHub Auth - app/github/auth.py
V4: Thread-safe installation token caching.

FIXED (BUG 10 + LOOPHOLE 5):
  Added threading.Lock to prevent race condition.
  Old bug: Thread A and Thread B both see cache miss simultaneously
           → both make token requests → 1 wasted API call + JWT generation.
  Fix: Lock ensures only one thread fetches at a time.

Token validity: GitHub issues 1-hour tokens.
Cache duration: 50 minutes (10 min buffer before expiry).
"""

import os
import time
import logging
import threading

import jwt
import requests

log = logging.getLogger(__name__)

APP_ID = os.environ.get("GITHUB_APP_ID", "")
PRIVATE_KEY = os.environ.get("GITHUB_PRIVATE_KEY", "").replace("\\n", "\n")

_token_cache: dict = {}

# Two levels, deliberately.
#
# `_cache_lock` guards the dictionaries themselves and is never held across a
# network call. `_fetch_locks` holds one lock per installation, so exactly one
# thread fetches a given installation's token while every other installation —
# and every cache hit — proceeds untouched.
#
# A single global lock previously wrapped the whole function including the
# 15-second token POST. That does prevent the duplicate-fetch race it was
# written for, but it also means a cache *hit* for installation B waits on a
# cold *fetch* for installation A. With 8 gunicorn threads and several
# installations, one slow response from GitHub serialises all GitHub work in
# the process.
_cache_lock = threading.Lock()
_fetch_locks: dict[int, threading.Lock] = {}


def _fetch_lock_for(installation_id: int) -> threading.Lock:
    with _cache_lock:
        lock = _fetch_locks.get(installation_id)
        if lock is None:
            lock = threading.Lock()
            _fetch_locks[installation_id] = lock
        return lock


def _cached_token(installation_id: int) -> str:
    """A token with more than 5 minutes left, or "". Holds the lock briefly."""
    with _cache_lock:
        cached = _token_cache.get(installation_id)
    if cached and cached["expires"] > time.time() + 300:
        return cached["token"]
    return ""


def get_jwt() -> str:
    """Generate a short-lived JWT for authenticating as the GitHub App."""
    now = int(time.time())
    payload = {
        "iat": now - 60,  # Issued 60s ago (clock skew tolerance)
        "exp": now + 540,  # Expires in 9 min (GitHub allows max 10 min)
        "iss": APP_ID,
    }
    token = jwt.encode(payload, PRIVATE_KEY, algorithm="RS256")
    return token if isinstance(token, str) else token.decode("utf-8")


def get_installation_token(installation_id: int) -> str:
    """
    Returns a valid installation access token, cached for 50 of its 60 minutes.

    Thread-safe, and only ever serialises threads that want the *same*
    installation: a cache hit never waits, and a cold fetch for one
    installation does not block another. See the lock notes above.
    """
    token = _cached_token(installation_id)
    if token:
        return token

    # Only threads wanting THIS installation queue here.
    with _fetch_lock_for(installation_id):
        # Re-check: while waiting for this lock, the thread ahead of us very
        # likely populated the cache. Without this the queue behind a cold
        # start makes one redundant token request per waiting thread.
        token = _cached_token(installation_id)
        if token:
            return token

        app_jwt = get_jwt()
        r = requests.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github.v3+json",
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        token = data["token"]

        entry = {
            "token": token,
            "expires": time.time() + 3000,  # 50 of the token's 60 minutes
            # GitHub reports what the installation was actually GRANTED, and
            # this response was throwing it away. It is the only authoritative
            # answer to "why did that command say it could not check my
            # permission" — the alternative is guessing at which checkbox the
            # operator missed. See app/core/preflight.py.
            "permissions": data.get("permissions") or {},
        }
        with _cache_lock:
            _token_cache[installation_id] = entry
        log.info(f"auth.token_fetched installation_id={installation_id}")
        return token


def get_installation_permissions(installation_id: int) -> dict:
    """
    What GitHub says this installation was granted, e.g. {"issues": "write"}.

    Populated by the token exchange, so it costs nothing extra on a warm cache
    and one ordinary token fetch on a cold one. Returns {} if the exchange
    fails — callers treat that as "unknown", never as "nothing granted".
    """
    try:
        get_installation_token(installation_id)
    except Exception as e:
        log.warning(f"auth.permissions_unavailable installation_id={installation_id}: {e}")
        return {}
    with _cache_lock:
        entry = _token_cache.get(installation_id) or {}
    return dict(entry.get("permissions") or {})


def clear_token_cache(installation_id: int = None):
    """
    Force-clear cached token(s).
    Call with no args to clear all, or pass an ID to clear one.
    Useful in tests and when GitHub App is reinstalled.
    """
    with _cache_lock:
        if installation_id is not None:
            _token_cache.pop(installation_id, None)
            _fetch_locks.pop(installation_id, None)
            log.debug(f"auth.cache_cleared installation_id={installation_id}")
        else:
            _token_cache.clear()
            _fetch_locks.clear()
            log.debug("auth.cache_cleared all")
