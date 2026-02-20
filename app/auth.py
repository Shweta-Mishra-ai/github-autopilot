"""
GitHub App Auth
Gets installation access tokens so we can act on user repos.
"""

import os
import time
import jwt
import requests
import logging

log = logging.getLogger(__name__)

APP_ID = os.environ.get("GITHUB_APP_ID", "")
PRIVATE_KEY = os.environ.get("GITHUB_PRIVATE_KEY", "").replace("\\n", "\n")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

# Cache tokens per installation
_token_cache = {}


def get_jwt() -> str:
    """Generate a JWT for authenticating as the GitHub App."""
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + (10 * 60),
        "iss": APP_ID
    }
    return jwt.encode(payload, PRIVATE_KEY, algorithm="RS256")


def get_installation_token(installation_id: int) -> str:
    """Get an installation access token (cached, refreshed every 50 min)."""
    cached = _token_cache.get(installation_id)
    if cached and cached["expires"] > time.time() + 60:
        return cached["token"]

    app_jwt = get_jwt()
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github.v3+json"
    }
    r = requests.post(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        headers=headers, timeout=15
    )
    r.raise_for_status()
    data = r.json()
    token = data["token"]

    _token_cache[installation_id] = {
        "token": token,
        "expires": time.time() + (50 * 60)
    }
    log.info(f"Got fresh token for installation {installation_id}")
    return token


def gh_get(path: str, token: str) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json"
    }
    r = requests.get(f"https://api.github.com{path}", headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()


def gh_post(path: str, token: str, data: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json"
    }
    r = requests.post(f"https://api.github.com{path}", headers=headers, json=data, timeout=20)
    r.raise_for_status()
    return r.json()


def gh_patch(path: str, token: str, data: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json"
    }
    r = requests.patch(f"https://api.github.com{path}", headers=headers, json=data, timeout=20)
    r.raise_for_status()
    return r.json()


def groq_ask(system: str, user: str, max_tokens: int = 1500, fast: bool = False) -> dict:
    """Call Groq AI and return parsed JSON."""
    import re, json as _json
    model = "llama-3.1-8b-instant" if fast else "llama-3.3-70b-versatile"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user}
        ]
    }
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers=headers, json=payload, timeout=45
    )
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"]
    match = re.search(r'\{[\s\S]*\}', text)
    if match:
        return _json.loads(match.group())
    return {"raw": text}


def groq_text(system: str, user: str, max_tokens: int = 800) -> str:
    """Call Groq and return plain text."""
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "llama-3.1-8b-instant",
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user}
        ]
    }
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers=headers, json=payload, timeout=30
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()
