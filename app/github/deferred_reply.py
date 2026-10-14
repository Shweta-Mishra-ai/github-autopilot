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
    from app.github.helpers import fit_comment

    body = fit_comment(body)
    try:
        post(f"/repos/{repo}/issues/{number}/comments", token, {"body": body})
        return
    except Exception as e:
        if not is_transient(e) or not installation_id:
            raise
        from app.core.event_queue import schedule

        delay = max(_FIRST_DELAY, int(getattr(e, "retry_after", 0) or 0))
        job_id = "reply-" + hashlib.sha256(f"{repo}#{number}:{body}".encode()).hexdigest()[:16]
        payload = {"repo": repo, "number": number, "installation_id": installation_id, "body": body}
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
    gh_post(f"/repos/{repo}/issues/{number}/comments", token, {"body": payload["body"]})
    log.info(f"reply.delivered repo={repo} number={number}")
