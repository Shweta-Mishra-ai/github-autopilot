"""
app/core/learning.py
What maintainers did with the bot's autofix PRs: merged, or closed unmerged.

Both outcomes are recorded from the PR's `closed` event
(handlers/pull_request/outcomes.py) and shown beside the next autofix
(autofix._track_record).

This module also used to hold a "learned conventions" store, read into the
/fix prompt as "Repo conventions (learned from previously accepted fixes)",
and a fix acceptance rate. Nothing ever wrote a convention, so that prompt
line was always empty; and nothing recorded an ignored fix, so the rate
could only read 0.5 (the placeholder) or 1.0. Both were removed rather than
left implying a learning loop that did not exist. What the bot actually
learns from outcomes reaches prompts through repository memory.
"""

import logging

from app.core.redis_client import get_redis

log = logging.getLogger(__name__)
LEARNING_TTL = 90 * 86400  # 90 days


def record_autofix_merged(repo: str, pr_number: int, issue_number: int):
    """Bot-created autofix PR was merged."""
    _incr(f"learn:{repo}:autofix_merged")


def record_autofix_closed(repo: str, pr_number: int):
    """Bot-created autofix PR was closed without merging."""
    _incr(f"learn:{repo}:autofix_closed")


def get_learning_summary(repo: str) -> dict:
    """
    Autofix outcomes for this repo. Raises when Redis cannot be read: zeros
    would be a claim that nothing was merged or rejected.
    """
    r = get_redis()
    return {
        "autofix_merged": int(r.get(f"learn:{repo}:autofix_merged") or 0),
        "autofix_closed": int(r.get(f"learn:{repo}:autofix_closed") or 0),
    }


def _incr(key: str):
    try:
        r = get_redis()
        r.incr(key)
        r.expire(key, LEARNING_TTL)
    except Exception as e:
        log.warning(f"learning.incr_failed key={key}: {e}")
