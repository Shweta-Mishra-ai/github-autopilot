"""
app/handlers/pull_request/outcomes.py
What happened to the bot's own autofix PRs — merged or closed unmerged.

The learning store had record_autofix_closed() with no caller, and recorded a
merge only when it went through /merge — a fix merged with GitHub's button,
the usual way, taught the bot nothing, and a rejected one never registered at
all. So every "learning" figure counted successes only.

A pull request's `closed` event carries both outcomes, whoever closed it and
however it was merged, so both are recorded here and nowhere else.
"""

from __future__ import annotations

import contextlib
import re

AUTOFIX_BRANCH = "fix/bot-issue-"


def record_autofix_outcome(payload: dict) -> bool:
    """
    Record a closed autofix PR's outcome. True when it was one. Never raises:
    a learning write must not cost the event anything.
    """
    pr = payload.get("pull_request") or {}
    branch = (pr.get("head") or {}).get("ref") or ""
    if payload.get("action") != "closed" or not branch.startswith(AUTOFIX_BRANCH):
        return False

    repo = (payload.get("repository") or {}).get("full_name", "")
    number = pr.get("number", 0)

    # The branch name alone proved nothing: anyone could open a PR from a
    # fork branch named `fix/bot-issue-1`, close it, and count a rejected
    # autofix — and have their PR title written into repository memory,
    # which is later recalled into prompts. A real autofix PR is opened by
    # this App (/apply) from a branch in this repository.
    head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name", "")
    if head_repo != repo or (pr.get("user") or {}).get("type") != "Bot":
        return False

    # Once per PR: closing, reopening and closing again is one outcome.
    try:
        from app.core.redis_client import get_redis

        if not get_redis().set(f"autofix_outcome:{repo}:{number}", "1", nx=True, ex=86400 * 365):
            return False
    except Exception:
        return False  # unrecordable now; better uncounted than counted twice
    m = re.search(r"issue-(\d+)", branch)
    issue = int(m.group(1)) if m else 0
    title = str(pr.get("title") or "")[:150]

    with contextlib.suppress(Exception):
        from app.core import learning

        if pr.get("merged"):
            learning.record_autofix_merged(repo, number, issue)
        else:
            learning.record_autofix_closed(repo, number)

    with contextlib.suppress(Exception):
        from app.intelligence.memory import remember

        verdict = "merged" if pr.get("merged") else "closed WITHOUT merging (rejected)"
        remember(
            repo,
            f"Autofix PR #{number} for issue #{issue} was {verdict}: {title}",
            kind="fix",
            meta={"pr": number, "issue": issue, "merged": bool(pr.get("merged"))},
        )
    return True
