"""
GitHub Auth - app/github/auth.py
JWT generation and installation token caching.
"""

import os
import time
import logging
import jwt
import requests

log = logging.getLogger(__name__)

APP_ID = os.environ.get("GITHUB_APP_ID", "")
PRIVATE_KEY = os.environ.get("GITHUB_PRIVATE_KEY", "").replace("\\n", "\n")

_token_cache: dict = {}


def get_jwt() -> str:
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 540, "iss": APP_ID}
    token = jwt.encode(payload, PRIVATE_KEY, algorithm="RS256")
    return token if isinstance(token, str) else token.decode("utf-8")


def get_installation_token(installation_id: int) -> str:
    """Returns cached token or fetches a fresh one."""
    cached = _token_cache.get(installation_id)
    if cached and cached["expires"] > time.time() + 120:
        return cached["token"]

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
    token = r.json()["token"]

    _token_cache[installation_id] = {
        "token": token,
        "expires": time.time() + 3000,   # 50 min
    }
    log.info(f"Fetched installation token for {installation_id}")
    return token
