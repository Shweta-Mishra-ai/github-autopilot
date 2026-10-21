"""
app/core/repo_identity.py
Per-repository state is keyed by name; this makes sure a name still means the
same repository.

Memory, learning and analytics counters, rollback snapshots, the open
secret-alert issue number and the per-PR "already done" markers are all
stored under `owner/name`. A name is not an identity: a repository can be
deleted or renamed and another created under the old name — by a different
owner, if the account itself was renamed and the login re-registered. The new
repository then inherited the old one's state: its memory recalled into
prompts, its snapshots offered to /rollback, its alert issue number used for
comments.

GitHub's numeric repository id does not change. The id last seen for each
name is recorded, and when an event arrives with a different id for that name,
the name's state is cleared before anything reads it. A rename leaves the old
name's state to expire on its own TTLs; the new name starts empty.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

ID_TTL_SECONDS = 400 * 86400  # longer than any state it guards

# Keys stored under a repository name, as glob patterns. {repo} is substituted;
# GitHub names cannot contain glob metacharacters.
_EXACT = (
    "mem:{repo}",
    "mem:hashes:{repo}",
    "snapshot_index:{repo}",
    "secret_alert:{repo}",
    "readme_pr:{repo}",
    "labels_ensured:{repo}",
)
_PREFIXES = (
    "learn:{repo}:*",
    "analytics:{repo}:*",
    "snapshot:{repo}:*",
    "snapshot_actions:{repo}:*",
    "snapshot_activity:{repo}:*",
    "snapshot_rolled_back:{repo}:*",
    "push_secret_seen:{repo}:*",
    "push_reported:{repo}:*",
    "ci_fail:{repo}:*",
    "ci_alert:{repo}:*",
    "pr_risk:{repo}:*",
    "pr_opened:{repo}#*",
    "pr_metadata:{repo}#*",
    "pr_high_risk:{repo}#*",
    "pr_sections:{repo}#*",
    "autofix_outcome:{repo}:*",
    "commit_msg_suggested:{repo}:*",
    # Today's AI budget and command rate limits: a new repository under the
    # name started the day with the old one's usage counted against it.
    "limit:{repo}:*",
    "cmd_rl:{repo}:*",
)


def _key(repo: str) -> str:
    return f"repo_id:{repo}"


def ensure(repo: str, repo_id) -> bool:
    """
    Record `repo_id` for `repo`; clear the name's state first when the name
    last belonged to a different repository. True when state was cleared.

    Raises when Redis cannot be read: the caller decides whether to go on.
    """
    if not repo or not repo_id:
        return False
    from app.core.redis_client import get_redis

    r = get_redis()
    current = str(repo_id)
    seen = r.get(_key(repo))
    if isinstance(seen, bytes):
        seen = seen.decode()
    cleared = False
    if seen is not None and str(seen) != current:
        n = purge(repo)
        log.warning(
            f"repo_identity.name_reused repo={repo} old_id={seen} new_id={current} "
            f"— cleared {n} key(s) left by the previous repository"
        )
        cleared = True
    r.set(_key(repo), current, ex=ID_TTL_SECONDS)
    return cleared


def purge(repo: str) -> int:
    """Delete every key stored under `repo`'s name. Returns how many."""
    from app.core.redis_client import get_redis

    r = get_redis()
    keys = [p.format(repo=repo) for p in _EXACT]
    for pattern in _PREFIXES:
        keys.extend(r.scan_iter(match=pattern.format(repo=repo), count=500))
    deleted = 0
    for k in set(keys):
        deleted += int(r.delete(k) or 0)
    r.srem("mem:repos", repo)
    try:
        from app.core.config import invalidate_config_cache

        invalidate_config_cache(repo)
    except Exception as e:  # nosec B110 - the cache expires on its own in minutes
        log.debug(f"repo_identity.config_cache_not_cleared repo={repo}: {e}")
    return deleted
