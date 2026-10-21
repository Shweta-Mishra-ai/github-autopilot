"""
A reply that GitHub refused for a transient reason is delivered later, by
itself.

A command's reply used to be posted once: a 502 or a secondary rate limit on
that POST was logged and the reply was gone, while the person who typed the
command waited for an answer that never came. Retrying the whole event is not
the answer either — /merge, /release and /apply would repeat their writes to
deliver a sentence about them. So the reply text is parked on the queue as its
own job, and only the POST is retried.
"""

import hashlib
import logging

from app.core.transient import is_transient
from app.github.client import gh_post

log = logging.getLogger(__name__)

# Not a GitHub event name; server.webhook() refuses it from outside.
REPLY_EVENT = "autopilot.reply"
INTERNAL_PREFIX = "autopilot."

_FIRST_DELAY = 30

# In every reply. A timeout or a 5xx on a POST says the answer was lost, not
# that the comment was not created; the deferred delivery looks for this
# before posting, or the same reply ("✅ Merged!") appeared twice.
_MARKER = "<!-- autopilot-reply:{} -->"


def _already_posted(repo: str, number: int, token: str, marker: str, since: str) -> bool:
    """True when a comment carrying `marker` exists. Raises when the comments
    cannot be read: the queue retries, rather than risk a duplicate."""
    from app.github.client import gh_get

    query = f"?per_page=100&since={since}" if since else "?per_page=100"
    comments = gh_get(f"/repos/{repo}/issues/{number}/comments{query}", token) or []
    return any(marker in str(c.get("body") or "") for c in comments if isinstance(c, dict))


def post_comment(
    repo: str, number: int, token: str, installation_id: int, body: str, post=gh_post
) -> None:
    """
    Post an issue/PR comment; on a transient failure, schedule it for later.

    Raises when the reply is lost: a non-transient refusal (the issue is
    locked, the App lost access), or a transient one with no queue to park
    it on. A caller that catches nothing lets the event runner count it.
    `post` is the caller's own gh_post, so the caller's patch target holds.
    """
    from datetime import datetime, timedelta, timezone

    from app.github.helpers import fit_comment

    digest = hashlib.sha256(f"{repo}#{number}:{body}".encode()).hexdigest()[:16]
    marker = _MARKER.format(digest)
    body = f"{fit_comment(body, reserve=len(marker) + 1)}\n{marker}"
    started = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        post(f"/repos/{repo}/issues/{number}/comments", token, {"body": body})
        return
    except Exception as e:
        if not is_transient(e) or not installation_id:
            raise
        from app.core.event_queue import schedule

        delay = max(_FIRST_DELAY, int(getattr(e, "retry_after", 0) or 0))
        job_id = "reply-" + digest
        payload = {
            "repo": repo,
            "number": number,
            "installation_id": installation_id,
            "body": body,
            "marker": marker,
            "since": started,
        }
        if not schedule(REPLY_EVENT, payload, repo, delay, job_id):
            raise
        log.warning(
            f"reply.deferred repo={repo} number={number} in={delay}s: {type(e).__name__}: {e}"
        )


def deliver(payload: dict) -> None:
    """Run a parked reply. Raises on failure so the queue retries or dead-letters it."""
    from app.github.auth import get_installation_token

    repo = payload["repo"]
    number = int(payload["number"])
    token = get_installation_token(int(payload["installation_id"]))
    marker = payload.get("marker")
    if marker and _already_posted(repo, number, token, marker, payload.get("since", "")):
        log.info(f"reply.already_delivered repo={repo} number={number} — not posted again")
        return
    gh_post(f"/repos/{repo}/issues/{number}/comments", token, {"body": payload["body"]})
    log.info(f"reply.delivered repo={repo} number={number}")
