"""
GitHub Client - app/github/client.py
V4 changes:

FIXED (BUG 3): Added gh_patch() method.
  /apply was calling gh_post() to update a branch ref — WRONG.
  GitHub API requires PATCH to update an existing ref, not POST.
  POST creates a new ref → fails with 422 if ref already exists.

FIXED (LOOPHOLE 6): Handle secondary rate limit (403 with abuse message).
  Old code only handled 429. A 403 secondary rate limit caused unhandled exception.

NEW (LOOPHOLE 7): gh_get_all() auto-paginates.
  Old: gh_get(...?per_page=50) — missed items 51+.
  Health score, stale check etc were working on incomplete data.
"""

import time
import logging
import requests

from app.github.rate_limit import update_from_headers, check_and_wait

log = logging.getLogger(__name__)

GITHUB_API      = "https://api.github.com"
DEFAULT_TIMEOUT = 20


class GitHubError(Exception):
    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _handle_response(r: requests.Response, method: str, path: str):
    """Parse response, update rate limit state, raise on errors."""
    update_from_headers(dict(r.headers))

    if r.status_code in (200, 201):
        return r.json() if r.content else {}
    if r.status_code == 204:
        return {}

    # Primary rate limit
    if r.status_code == 429:
        retry_after = int(r.headers.get("Retry-After", 30))
        raise GitHubError(f"Primary rate limit — retry after {retry_after}s", 429)

    # ✅ FIXED: Secondary rate limit (LOOPHOLE 6)
    if r.status_code == 403:
        try:
            body = r.json()
            msg  = body.get("message", "").lower()
            if "secondary rate limit" in msg or "abuse" in msg:
                log.warning(f"github.secondary_rate_limit path={path} — waiting 60s")
                time.sleep(60)
                raise GitHubError("Secondary rate limit — waited 60s, retry now", 403)
            raise GitHubError(f"Forbidden: {body.get('message', 'no message')}", 403)
        except GitHubError:
            raise
        except Exception:
            raise GitHubError(f"403 Forbidden: {path}", 403)

    if r.status_code == 404:
        raise GitHubError(f"Not found: {path}", 404)

    if r.status_code == 422:
        try:
            detail = r.json().get("message", "Unprocessable Entity")
        except Exception:
            detail = "Unprocessable Entity"
        raise GitHubError(f"422 Unprocessable: {detail}", 422)

    raise GitHubError(
        f"{method} {path} → {r.status_code}: {r.text[:200]}",
        r.status_code,
    )


# ── Core HTTP methods ─────────────────────────────────────────────────────────

def gh_get(path: str, token: str) -> dict | list:
    check_and_wait()
    url = path if path.startswith("http") else f"{GITHUB_API}{path}"
    r = requests.get(url, headers=_headers(token), timeout=DEFAULT_TIMEOUT)
    return _handle_response(r, "GET", path)


def gh_get_all(path: str, token: str, max_pages: int = 5) -> list:
    """
    ✅ NEW (LOOPHOLE 7): Auto-paginate. Returns ALL results across pages.
    Use instead of gh_get() when you need complete lists
    (issues, PRs, commits, contributors).

    Example:
        issues = gh_get_all("/repos/org/repo/issues?state=open", token)
        # Returns ALL open issues, not just first 50
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
                break   # Last page — fewer than 100 items returned
        else:
            return data  # Single object, not a list

    return results


def gh_post(path: str, token: str, data: dict) -> dict:
    check_and_wait()
    url = f"{GITHUB_API}{path}"
    r   = requests.post(url, headers=_headers(token), json=data, timeout=DEFAULT_TIMEOUT)
    return _handle_response(r, "POST", path)


def gh_put(path: str, token: str, data: dict) -> dict:
    check_and_wait()
    url = f"{GITHUB_API}{path}"
    r   = requests.put(url, headers=_headers(token), json=data, timeout=DEFAULT_TIMEOUT)
    return _handle_response(r, "PUT", path)


def gh_patch(path: str, token: str, data: dict) -> dict:
    """
    ✅ NEW (BUG 3): PATCH method for updating existing resources.
    Required for:
      - Updating branch refs: PATCH /repos/{owner}/{repo}/git/refs/heads/{branch}
      - Updating PR details:  PATCH /repos/{owner}/{repo}/pulls/{number}
      - Updating issues:      PATCH /repos/{owner}/{repo}/issues/{number}
    POST would create a new resource — wrong for updates.
    """
    check_and_wait()
    url = f"{GITHUB_API}{path}"
    r   = requests.patch(url, headers=_headers(token), json=data, timeout=DEFAULT_TIMEOUT)
    return _handle_response(r, "PATCH", path)


def gh_delete(path: str, token: str) -> dict:
    check_and_wait()
    url = f"{GITHUB_API}{path}"
    r   = requests.delete(url, headers=_headers(token), timeout=DEFAULT_TIMEOUT)
    return _handle_response(r, "DELETE", path)
