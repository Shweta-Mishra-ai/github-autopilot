"""
app/core/cache.py
Redis cache for GitHub API reads.

Scope is deliberately narrow. Caching a GitHub read is only safe when a stale
answer cannot change a decision, and most reads in this codebase fail that test:

  - PR files and diffs change on every push, and the review must see the push
    that triggered it.
  - `/contents/` is read at a specific PR head by the security scan; serving a
    cached copy from a different ref would report the wrong file.
  - Issue and comment bodies drive command handling and must be current.

Repository *metadata* — default branch, archived flag, primary language — is
different: it changes about never, and every caller treats it as background
information. That is what `get_repo_metadata()` caches, and it is the only
cached read wired into the app. `cached_gh_get()` remains available for a
caller that has reasoned about staleness itself.

Isolation: cache keys include a digest of the installation token, so two
installations never share an entry. That digest is 64 bits, not the 32 it
started with — at 32 bits a birthday collision between installations becomes
plausible, and a collision here means one tenant reading another's response.
"""

import contextlib
import hashlib
import json
import logging

from app.core import redis_client

log = logging.getLogger(__name__)

_KEY_PREFIX = "ghcache:data:"
_STATS_HITS = "ghcache:stats:hits"
_STATS_MISSES = "ghcache:stats:misses"

# Sentinel so a cached `null` is a hit rather than being retried forever.
_MISS = object()

# Repository metadata TTL. Ten minutes rather than an hour: a default-branch
# rename is rare but not impossible, and `/apply` opens PRs against that branch.
# Ten minutes still collapses essentially every burst of events on a repo.
REPO_METADATA_TTL = 600

# Per-path TTLs for the general-purpose helper. Only slow-changing, read-mostly
# paths appear here on purpose — a path absent from this map still works but
# gets the conservative default, and nothing correctness-critical should be
# routed through here at all.
TTL_MAP = {
    "/repos/": REPO_METADATA_TTL,
    "default": 180,
}


def get_repo_metadata(repo: str, token: str) -> dict:
    """
    Cached read of `/repos/{repo}`.

    The one cached read wired into the app. Callers use it for `default_branch`,
    `archived` and `language`; a value up to REPO_METADATA_TTL seconds stale is
    harmless for all three.

    Falls through to a live call on any cache problem, so a Redis outage costs
    API quota rather than functionality. Never raises anything the equivalent
    `gh_get` would not.
    """
    return cached_gh_get(f"/repos/{repo}", token, ttl=REPO_METADATA_TTL)


def cached_gh_get(path: str, token: str, ttl: int = 0) -> dict | list | None:
    """Cache-backed gh_get. Falls back to a live call on miss."""
    key = _make_key(path, token)
    ttl = ttl or _get_ttl(path)

    cached = _get(key)
    if cached is not _MISS:
        return cached

    from app.github.client import gh_get

    data = gh_get(path, token)
    _set(key, data, ttl, path=path)
    return data


def invalidate(path: str, token: str) -> None:
    """Drop one cached path for one token."""
    _delete(_make_key(path, token))


def invalidate_repo(repo: str) -> int:
    """
    Drop every cached entry for a repository, across all tokens.

    Returns the number of keys removed. Cache keys hash the path, so the repo
    name is stored alongside rather than being recoverable from the key —
    without that index this could only be done by scanning and re-hashing,
    which is why it previously scanned for a substring that was never present
    and therefore never deleted anything.
    """
    index_key = _repo_index_key(repo)
    try:
        r = redis_client.get_redis()
        members = r.smembers(index_key) or set()
        keys = [m.decode() if isinstance(m, bytes) else str(m) for m in members]
        if keys:
            r.delete(*keys)
        r.delete(index_key)
        return len(keys)
    except Exception as e:
        log.warning(f"cache.invalidate_repo_failed repo={repo}: {e}")
        return 0


def get_stats() -> dict:
    """
    Hit/miss counters and the current entry count.

    Uses SCAN, not KEYS: against real Redis, KEYS walks the entire keyspace and
    blocks the server while it does so. On a shared free-tier instance that is
    a production hazard, not a micro-optimisation.
    """
    try:
        r = redis_client.get_redis()
        hits = int(r.get(_STATS_HITS) or 0)
        misses = int(r.get(_STATS_MISSES) or 0)
        total = hits + misses
        return {
            "hits": hits,
            "misses": misses,
            "hit_rate": round(hits / total, 3) if total else 0.0,
            "keys": sum(1 for _ in r.scan_iter(match=f"{_KEY_PREFIX}*")),
        }
    except Exception as e:
        log.debug(f"cache.stats_unavailable: {e}")
        return {"hits": 0, "misses": 0, "hit_rate": 0.0, "keys": 0}


# ── Internals ────────────────────────────────────────────────────────────────


def _make_key(path: str, token: str) -> str:
    # 64 bits of token digest, not 32: an entry is scoped to an installation,
    # and a collision would let one installation read another's cached data.
    th = hashlib.sha256(token.encode()).hexdigest()[:16]
    # usedforsecurity=False is required, not cosmetic: this is a cache-key
    # digest with no security property, and on a FIPS-mode interpreter a plain
    # hashlib.md5() raises ValueError rather than hashing — every cached GitHub
    # read would fail. The flag also tells SAST this is not a weak-crypto use.
    ph = hashlib.md5(path.encode(), usedforsecurity=False).hexdigest()[:12]
    return f"{_KEY_PREFIX}{th}:{ph}"


def _repo_index_key(repo: str) -> str:
    return f"ghcache:idx:{repo}"


def _repo_from_path(path: str) -> str:
    """`/repos/owner/name/pulls/1` -> `owner/name`, or "" if not a repo path."""
    parts = [p for p in path.split("?", 1)[0].split("/") if p]
    if len(parts) >= 3 and parts[0] == "repos":
        return f"{parts[1]}/{parts[2]}"
    return ""


def _get_ttl(path: str) -> int:
    for pattern, ttl in TTL_MAP.items():
        if pattern != "default" and pattern in path:
            return ttl
    return TTL_MAP["default"]


def _get(key: str):
    """Return the cached value, or the _MISS sentinel."""
    try:
        r = redis_client.get_redis()
        raw = r.get(key)
        if raw is None:
            r.incr(_STATS_MISSES)
            return _MISS
        r.incr(_STATS_HITS)
        return json.loads(raw)
    except Exception:
        # A cache failure must never break the read path.
        return _MISS


def _set(key: str, data, ttl: int, path: str = "") -> None:
    with contextlib.suppress(Exception):
        r = redis_client.get_redis()
        r.set(key, json.dumps(data), ex=ttl)
        # Index the key under its repository so invalidate_repo() can find it.
        # The key itself is a hash of the path, so the repo is not recoverable
        # from it — without this index, per-repo invalidation is impossible.
        # The index is given a longer TTL than the entries it points at, so it
        # cannot outlive them by much but never expires first and orphans them.
        repo = _repo_from_path(path)
        if repo:
            index_key = _repo_index_key(repo)
            r.sadd(index_key, key)
            r.expire(index_key, ttl * 2)


def _delete(key: str) -> None:
    with contextlib.suppress(Exception):
        redis_client.get_redis().delete(key)
