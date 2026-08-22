"""
app/core/installations.py — which installation can act on which repository.

An installation id arrives only on a webhook payload, and nothing persisted it,
so the app could act on a repository *while handling an event for it* and never
again. Anything that runs on a schedule rather than in response to an event —
the periodic full scan — therefore had no way to authenticate to any
repository at all.

This is a small registry, not a database: repo -> installation id, refreshed on
every event, expiring on its own so an uninstalled app stops being scanned
without anyone having to remember to clean up.

Only the id is stored. Installation *tokens* live in the in-process cache in
app/github/auth.py and expire in an hour; writing one to Redis would put a
credential in a store this app treats as cache and would outlive the token's
own lifetime.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

_INDEX_KEY = "installations:index"
_ENTRY_PREFIX = "installations:repo:"

# 45 days. Comfortably longer than the 15-day maintenance cadence, so a repo
# that is quiet between two scheduled runs is still scanned; short enough that
# an uninstalled app ages out rather than being scanned forever.
ENTRY_TTL_SECONDS = 45 * 24 * 3600


def _entry_key(repo: str) -> str:
    return f"{_ENTRY_PREFIX}{repo}"


def remember_installation(repo: str, installation_id: int) -> bool:
    """
    Record that `installation_id` can act on `repo`. Never raises.

    Called on every handled event, so it is a hot path: two writes, no reads.
    """
    if not repo or not installation_id:
        return False
    try:
        from app.core.redis_client import get_redis

        r = get_redis()
        r.set(_entry_key(repo), str(int(installation_id)), ex=ENTRY_TTL_SECONDS)
        r.sadd(_INDEX_KEY, repo)
        # The index must outlive the entries it points at, or it expires first
        # and orphans them; it is re-set on every event anyway.
        r.expire(_INDEX_KEY, ENTRY_TTL_SECONDS)
        return True
    except Exception as e:
        log.debug(f"installations.remember_failed repo={repo}: {e}")
        return False


def installation_for(repo: str) -> int | None:
    """The installation id for one repo, or None."""
    try:
        from app.core.redis_client import get_redis

        raw = get_redis().get(_entry_key(repo))
        if raw is None:
            return None
        return int(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception as e:
        log.debug(f"installations.lookup_failed repo={repo}: {e}")
        return None


def known_installations() -> dict[str, int]:
    """
    Every repo with a live installation id, as {repo: installation_id}.

    A repo in the index whose entry has expired is dropped from the index here
    rather than being returned with a null id — the caller of this function
    schedules work, and a repo it cannot authenticate to is not work it can do.
    """
    try:
        from app.core.redis_client import get_redis

        r = get_redis()
        members = r.smembers(_INDEX_KEY) or set()
    except Exception as e:
        log.debug(f"installations.index_read_failed: {e}")
        return {}

    live: dict[str, int] = {}
    stale: list[str] = []
    for m in members:
        repo = m.decode() if isinstance(m, bytes) else str(m)
        inst = installation_for(repo)
        if inst:
            live[repo] = inst
        else:
            stale.append(repo)

    if stale:
        try:
            from app.core.redis_client import get_redis

            get_redis().srem(_INDEX_KEY, *stale)
            log.debug(f"installations.index_pruned count={len(stale)}")
        except Exception:
            pass  # pruning is housekeeping; never fail the caller for it

    return live


def forget_installation(repo: str) -> None:
    """Drop one repo — used when GitHub tells us the app was uninstalled."""
    try:
        from app.core.redis_client import get_redis

        r = get_redis()
        r.delete(_entry_key(repo))
        r.srem(_INDEX_KEY, repo)
        log.info(f"installations.forgotten repo={repo}")
    except Exception as e:
        log.debug(f"installations.forget_failed repo={repo}: {e}")


def last_seen(repo: str) -> int:
    """Unix time of the most recent event for `repo`, or 0. Diagnostic only."""
    try:
        from app.core.redis_client import get_redis

        raw = get_redis().get(f"{_entry_key(repo)}:seen")
        return int(raw.decode() if isinstance(raw, bytes) else raw) if raw else 0
    except Exception:
        return 0


def touch(repo: str) -> None:
    """Record that we just saw an event for `repo`. Never raises."""
    try:
        from app.core.redis_client import get_redis

        get_redis().set(f"{_entry_key(repo)}:seen", str(int(time.time())), ex=ENTRY_TTL_SECONDS)
    except Exception:
        pass
