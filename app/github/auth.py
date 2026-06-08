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

# ✅ FIXED: Lock prevents race condition (LOOPHOLE 5)
_token_cache: dict = {}
_cache_lock = threading.Lock()


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
    Returns a valid installation access token.
    Thread-safe: uses Lock so only one thread fetches when cache misses.
    Cached for 50 minutes.
    """
    with _cache_lock:
        cached = _token_cache.get(installation_id)

        # Valid if exists AND has > 5 min remaining
        if cached and cached["expires"] > time.time() + 300:
            return cached["token"]

        # Fetch fresh token (inside lock — no other thread can race here)
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

        # Cache for 50 min (GitHub tokens last 60 min)
        _token_cache[installation_id] = {
            "token": token,
            "expires": time.time() + 3000,  # 50 * 60 = 3000 seconds
        }
        log.info(f"auth.token_fetched installation_id={installation_id}")
        return token


def clear_token_cache(installation_id: int = None):
    """
    Force-clear cached token(s).
    Call with no args to clear all, or pass an ID to clear one.
    Useful in tests and when GitHub App is reinstalled.
    """
    with _cache_lock:
        if installation_id is not None:
            _token_cache.pop(installation_id, None)
            log.debug(f"auth.cache_cleared installation_id={installation_id}")
        else:
            _token_cache.clear()
            log.debug("auth.cache_cleared all")
