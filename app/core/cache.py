"""
app/core/cache.py
V4 Sprint 6: Redis cache for GitHub API calls.

Reduces redundant API calls. Same PR files fetched 3x → fetched 1x cached.
"""

import hashlib
import json
import logging
from app.core import redis_client

log = logging.getLogger(__name__)

TTL_MAP = {
    "/pulls/":    300,
    "/issues/":   600,
    "/repos/":    1800,
    "/commits/":  3600,
    "/contents/": 300,
    "default":    180,
}


def cached_gh_get(path: str, token: str, ttl: int = 0) -> dict | list | None:
    """Cache-backed gh_get. Falls back to live API on miss."""
    key = _make_key(path, token)
    ttl = ttl or _get_ttl(path)

    cached = _get(key)
    if cached is not None:
        return cached

    from app.github.client import gh_get
    data = gh_get(path, token)
    _set(key, data, ttl)
    return data


def invalidate(path: str, token: str):
    _delete(_make_key(path, token))


def invalidate_repo(repo: str):
    try:
        r    = redis_client.get_redis()
        keys = r.keys(f"ghcache:*{repo}*")
        if keys:
            r.delete(*keys)
    except Exception:
        pass


def get_stats() -> dict:
    try:
        r = redis_client.get_redis()
        return {
            "hits":   int(r.get("ghcache:stats:hits") or 0),
            "misses": int(r.get("ghcache:stats:misses") or 0),
            "keys":   len(r.keys("ghcache:data:*")),
        }
    except Exception:
        return {"hits": 0, "misses": 0, "keys": 0}


def _make_key(path: str, token: str) -> str:
    th = hashlib.sha256(token.encode()).hexdigest()[:8]
    ph = hashlib.md5(path.encode()).hexdigest()[:12]
    return f"ghcache:data:{th}:{ph}"


def _get_ttl(path: str) -> int:
    for pattern, ttl in TTL_MAP.items():
        if pattern != "default" and pattern in path:
            return ttl
    return TTL_MAP["default"]


def _get(key: str):
    try:
        r   = redis_client.get_redis()
        raw = r.get(key)
        if raw:
            r.incr("ghcache:stats:hits")
            return json.loads(raw)
        r.incr("ghcache:stats:misses")
        return None
    except Exception:
        return None


def _set(key: str, data, ttl: int):
    try:
        redis_client.get_redis().set(key, json.dumps(data), ex=ttl)
    except Exception:
        pass


def _delete(key: str):
    try:
        redis_client.get_redis().delete(key)
    except Exception:
        pass
