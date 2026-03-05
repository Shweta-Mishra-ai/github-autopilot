"""
GitHub API Client - app/github/client.py
All GitHub API calls go through here.
Features: retry with exponential backoff, rate limit awareness, structured errors.
"""

import time
import logging
import requests
from app.github.rate_limit import update_from_headers, check_and_wait

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
DEFAULT_TIMEOUT = 20
MAX_RETRIES = 3


class GitHubError(Exception):
    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


def _request(method: str, path: str, token: str, data: dict = None,
             timeout: int = DEFAULT_TIMEOUT) -> dict:
    """
    Core request method. All public functions call this.
    Handles: retry, rate limit, error parsing, header tracking.
    """
    url = f"{GITHUB_API}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # Check rate limit before every call
    try:
        check_and_wait()
    except RuntimeError as e:
        raise GitHubError(str(e), status_code=429)

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.request(
                method, url,
                headers=headers,
                json=data,
                timeout=timeout
            )

            # Always update rate limit state from response headers
            update_from_headers(dict(response.headers))

            # Handle 429 rate limit — wait and retry
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 60))
                log.warning(f"GitHub 429 on {method} {path} — waiting {retry_after}s")
                time.sleep(min(retry_after, 120))
                continue

            # Handle 5xx server errors — retry with backoff
            if response.status_code >= 500:
                wait = 2 ** attempt
                log.warning(f"GitHub {response.status_code} on {method} {path} (attempt {attempt+1}) — retry in {wait}s")
                time.sleep(wait)
                last_error = GitHubError(
                    f"GitHub server error {response.status_code}",
                    status_code=response.status_code
                )
                continue

            # 4xx client errors — don't retry, raise immediately
            if response.status_code >= 400:
                try:
                    msg = response.json().get("message", response.text[:200])
                except Exception:
                    msg = response.text[:200]
                raise GitHubError(f"GitHub {response.status_code}: {msg}", status_code=response.status_code)

            # 204 No Content (e.g. DELETE)
            if response.status_code == 204:
                return {}

            return response.json()

        except GitHubError:
            raise
        except requests.exceptions.Timeout:
            last_error = GitHubError(f"Timeout on {method} {path}")
            log.warning(f"Timeout on {method} {path} (attempt {attempt+1})")
            time.sleep(2 ** attempt)
        except requests.exceptions.ConnectionError as e:
            last_error = GitHubError(f"Connection error: {e}")
            log.warning(f"Connection error on {method} {path} (attempt {attempt+1})")
            time.sleep(2 ** attempt)

    raise last_error or GitHubError(f"Failed after {MAX_RETRIES} attempts: {method} {path}")


# ── Public API ────────────────────────────────────────────────────────────────

def gh_get(path: str, token: str) -> dict:
    return _request("GET", path, token)


def gh_post(path: str, token: str, data: dict) -> dict:
    return _request("POST", path, token, data)


def gh_patch(path: str, token: str, data: dict) -> dict:
    return _request("PATCH", path, token, data)


def gh_put(path: str, token: str, data: dict) -> dict:
    return _request("PUT", path, token, data)


def gh_delete(path: str, token: str) -> bool:
    try:
        _request("DELETE", path, token)
        return True
    except GitHubError as e:
        if e.status_code == 404:
            return True   # Already deleted — that's fine
        log.warning(f"DELETE {path} failed: {e}")
        return False
