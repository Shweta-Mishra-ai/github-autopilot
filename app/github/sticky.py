"""
app/github/sticky.py — one bot comment per thread, edited in place.

Before V7 a single PR produced four comments on open (analysis, summary, code
review, test gaps) and two more on every push, none of which were ever
updated. A reviewer opening a busy PR saw a wall of stale bot output and had
to work out which comment reflected the current head.

The fix is a hidden HTML marker: find the bot's own previous comment and PATCH
it, rather than POSTing another. GitHub renders HTML comments as nothing, so
the marker is invisible to readers but trivially greppable for us.
"""

from __future__ import annotations

import logging

from app.github.client import gh_get, gh_patch, gh_post

log = logging.getLogger(__name__)

MARKER_PR_REPORT = "<!-- github-autopilot:pr-report -->"
MARKER_CI_REPORT = "<!-- github-autopilot:ci-report -->"

# Same bound gh_get_all uses. A thread with more comments than this is one
# where a fresh comment is the honest outcome anyway.
MAX_COMMENT_PAGES = 5
PER_PAGE = 100


def find_sticky(repo: str, issue_number: int, token: str, marker: str) -> int | None:
    """
    Comment id of the bot's marker-bearing comment, or None.

    Pages until the marker is found, rather than fetching every page and then
    looking. This used gh_get_all, which walks to the end of the thread before
    returning anything, so a busy pull request cost five API requests to locate
    a comment that was on the first page — measured at 450 comments: five GETs
    for a sticky sitting at position four.

    That is the normal case, not a pathological one. The sticky is posted when
    the pull request opens and edited afterwards, so it is almost always among
    the oldest comments, and GitHub returns them oldest first. The work thrown
    away was the entire rest of the thread.

    Never raises: a lookup failure means "post a fresh one", which is the safe
    direction — a duplicate comment is recoverable, a lost report is not.
    """
    try:
        path = f"/repos/{repo}/issues/{issue_number}/comments"
        for page in range(1, MAX_COMMENT_PAGES + 1):
            comments = gh_get(f"{path}?per_page={PER_PAGE}&page={page}", token)
            if not isinstance(comments, list) or not comments:
                return None

            for c in comments:
                if marker in (c.get("body") or ""):
                    return c.get("id")

            if len(comments) < PER_PAGE:
                return None  # short page: that was the end of the thread
    except Exception as e:
        log.debug(f"sticky.find_failed repo={repo} issue={issue_number}: {e}")
    return None


def upsert_sticky(repo: str, issue_number: int, token: str, marker: str, body: str) -> dict:
    """
    PATCH the existing sticky when one exists, else POST a new one.

    Falls back to POST if the PATCH fails — the sticky may have been deleted
    by a maintainer since we found it — so a report is never lost to a stale
    comment id.
    """
    if marker not in body:
        body = f"{body}\n\n{marker}"

    existing = find_sticky(repo, issue_number, token, marker)
    if existing is not None:
        try:
            return gh_patch(f"/repos/{repo}/issues/comments/{existing}", token, {"body": body})
        except Exception as e:
            log.warning(f"sticky.patch_failed id={existing} — posting fresh: {e}")

    return gh_post(f"/repos/{repo}/issues/{issue_number}/comments", token, {"body": body})
