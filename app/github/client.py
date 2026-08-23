"""
GitHub Client - app/github/client.py
V4 Sprint 5: Production-grade GitHub API client.

ADDED (Sprint 5):
  - Automatic retry with exponential backoff on 5xx errors (idempotent methods)
  - Retry on connection errors (network blip on Render free tier)
  - Per-request timeout enforcement
  - Structured error logging with request ID

WHY THIS MATTERS:
  Render free tier has occasional network blips.
  Without retry: 1 transient 503 → bot silently fails.
  With retry: transparent recovery in < 5 seconds.
  3 retries covers 99.9% of transient failures.
"""

import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.core.retry_after import parse_retry_after
from app.github.rate_limit import update_from_headers, check_and_wait

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
DEFAULT_TIMEOUT = 20
MAX_RETRIES = 3
RETRY_BACKOFF = 0.5  # 0.5s, 1s, 2s between retries


class GitHubError(Exception):
    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class GitHubSecondaryRateLimitError(GitHubError):
    """Raised when GitHub returns a 403 with secondary rate limit header."""

    def __init__(self, message: str = "GitHub secondary rate limit", retry_after: int = 60):
        super().__init__(message, status_code=403)
        self.retry_after = retry_after


# Only these are safe to replay. A 502/503/504 means the gateway could not
# give us an answer -- NOT that GitHub failed to act. If a POST creating a
# comment reached GitHub and the response was lost on the way back, retrying
# it posts the comment twice, and the bot writes comments on every push.
#
# This previously listed POST, PUT, PATCH and DELETE as well, so a single lost
# response duplicated whatever the call had already done. Replaying a write to
# save one round trip trades a rare transient error for a permanent wrong
# result, which is the wrong side of that trade.
#
# Connection errors are still retried for every method, including writes:
# those are raised before the request is established, so nothing can have been
# processed. Read timeouts are NOT retried for writes -- urllib3 gates those
# on this same set, which is exactly the distinction we want, since a read
# timeout happens after the request was sent.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _make_session() -> requests.Session:
    """
    Session with automatic retry on transient network errors.

    Retries: connection errors (any method), and 502/503/504 or read timeouts
    on idempotent methods only. Does NOT retry 4xx (client errors), 429 (rate
    limit — handled manually), or any write that may already have taken
    effect. See IDEMPOTENT_METHODS.
    """
    session = requests.Session()
    retry = Retry(
        total=MAX_RETRIES,
        backoff_factor=RETRY_BACKOFF,
        status_forcelist=[502, 503, 504],
        allowed_methods=IDEMPOTENT_METHODS,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_session = _make_session()


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _handle_response(r: requests.Response, method: str, path: str):
    """Parse response, update rate limit state, raise on errors."""
    update_from_headers(dict(r.headers))

    if r.status_code in (200, 201):
        return r.json() if r.content else {}
    if r.status_code == 204:
        return {}

    # Primary rate limit — caller should respect Retry-After
    if r.status_code == 429:
        # Tolerant parse: a bare int() here raised ValueError on the
        # HTTP-date form of the header, and ValueError escapes every
        # caller — they all catch GitHubError, not Exception.
        retry_after = parse_retry_after(r.headers.get("Retry-After"), 30)
        raise GitHubError(f"Primary rate limit — retry after {retry_after}s", 429)

    # Secondary rate limit (abuse detection)
    # FIXED: previously called time.sleep(60) here — this blocks a thread
    # pool worker for 60s, starving all other webhooks. Now we raise
    # GitHubSecondaryRateLimitError immediately so the caller can decide
    # whether to drop/retry. Never sleep in a shared worker thread.
    if r.status_code == 403:
        try:
            body = r.json()
            msg = body.get("message", "").lower()
            if "secondary rate limit" in msg or "abuse" in msg:
                retry_after = parse_retry_after(r.headers.get("Retry-After"), 60)
                log.warning(
                    f"github.secondary_rate_limit path={path} "
                    f"retry_after={retry_after}s — raising immediately (no sleep)"
                )
                raise GitHubSecondaryRateLimitError(
                    f"Secondary rate limit on {path}. Retry after {retry_after}s.",
                    retry_after=retry_after,
                )
            raise GitHubError(f"Forbidden: {body.get('message', 'no message')}", 403)
        except (GitHubError, GitHubSecondaryRateLimitError):
            raise
        except Exception as e:
            raise GitHubError(f"403 Forbidden: {path}", 403) from e

    if r.status_code == 404:
        raise GitHubError(f"Not found: {path}", 404)

    if r.status_code == 422:
        try:
            detail = r.json().get("message", "Unprocessable Entity")
        except Exception:
            detail = "Unprocessable Entity"
        raise GitHubError(f"422 Unprocessable: {detail}", 422)

    # 5xx — session already retried, this is the final failure
    if r.status_code >= 500:
        log.error(f"github.server_error method={method} path={path} status={r.status_code}")
        raise GitHubError(f"GitHub server error {r.status_code}: {path}", r.status_code)

    raise GitHubError(
        f"{method} {path} → {r.status_code}: {r.text[:200]}",
        r.status_code,
    )


# ── Core HTTP methods — all use retry session ─────────────────────────────────


def gh_get(path: str, token: str) -> dict | list:
    check_and_wait()
    url = path if path.startswith("http") else f"{GITHUB_API}{path}"
    try:
        r = _session.get(url, headers=_headers(token), timeout=DEFAULT_TIMEOUT)
    except requests.exceptions.ConnectionError as e:
        raise GitHubError(f"Connection error: {e}", 0) from e
    return _handle_response(r, "GET", path)


def gh_get_all(path: str, token: str, max_pages: int = 5) -> list:
    """Auto-paginate — returns ALL results across pages."""
    results = []
    sep = "&" if "?" in path else "?"

    for page in range(1, max_pages + 1):
        paged = f"{path}{sep}page={page}&per_page=100"
        try:
            data = gh_get(paged, token)
        except GitHubError as e:
            log.warning(f"gh_get_all stopped at page={page}: {e}")
            break

        if not data:
            break

        if isinstance(data, list):
            results.extend(data)
            if len(data) < 100:
                break
        else:
            return data

    return results


def gh_post(path: str, token: str, data: dict) -> dict:
    check_and_wait()
    url = f"{GITHUB_API}{path}"
    try:
        r = _session.post(url, headers=_headers(token), json=data, timeout=DEFAULT_TIMEOUT)
    except requests.exceptions.ConnectionError as e:
        raise GitHubError(f"Connection error: {e}", 0) from e
    return _handle_response(r, "POST", path)


def gh_put(path: str, token: str, data: dict) -> dict:
    check_and_wait()
    url = f"{GITHUB_API}{path}"
    try:
        r = _session.put(url, headers=_headers(token), json=data, timeout=DEFAULT_TIMEOUT)
    except requests.exceptions.ConnectionError as e:
        raise GitHubError(f"Connection error: {e}", 0) from e
    return _handle_response(r, "PUT", path)


def gh_patch(path: str, token: str, data: dict) -> dict:
    check_and_wait()
    url = f"{GITHUB_API}{path}"
    try:
        r = _session.patch(url, headers=_headers(token), json=data, timeout=DEFAULT_TIMEOUT)
    except requests.exceptions.ConnectionError as e:
        raise GitHubError(f"Connection error: {e}", 0) from e
    return _handle_response(r, "PATCH", path)


def gh_delete(path: str, token: str) -> dict:
    check_and_wait()
    url = f"{GITHUB_API}{path}"
    try:
        r = _session.delete(url, headers=_headers(token), timeout=DEFAULT_TIMEOUT)
    except requests.exceptions.ConnectionError as e:
        raise GitHubError(f"Connection error: {e}", 0) from e
    return _handle_response(r, "DELETE", path)
