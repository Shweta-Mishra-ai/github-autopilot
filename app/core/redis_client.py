"""
app/core/redis_client.py — Thread-safe Redis singleton; fails loud in production.
"""

from __future__ import annotations

import os
import logging
import threading
import redis as redis_lib

log = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "")
_IS_PRODUCTION = (
    os.environ.get("FLASK_ENV", "") == "production"
    or os.environ.get("ENVIRONMENT", "") == "production"
)

_pool: redis_lib.ConnectionPool | None = None
_client = None
_client_lock = threading.Lock()


def get_redis() -> "redis_lib.Redis | _FakeRedis":
    """
    Returns a Redis client backed by a shared connection pool.
    Thread-safe via double-checked locking.

    In production (FLASK_ENV=production): raises RuntimeError if Redis unavailable.
    In dev/test: falls back to _FakeRedis in-memory stub.
    """
    global _pool, _client

    if _client is not None:
        return _client

    with _client_lock:
        if _client is not None:
            return _client

        redis_url = os.environ.get("REDIS_URL", "")
        if not redis_url:
            if _IS_PRODUCTION:
                raise RuntimeError(
                    "REDIS_URL is not set in production environment. "
                    "Redis is required for idempotency and rate limiting. "
                    "Set REDIS_URL in your Render environment variables."
                )
            log.warning("REDIS_URL not set — using in-memory fallback (dev/test only)")
            _client = _FakeRedis()
            return _client

        try:
            _pool = redis_lib.ConnectionPool.from_url(
                redis_url,
                max_connections=10,
                socket_connect_timeout=5,
                socket_timeout=5,
                retry_on_timeout=True,
                decode_responses=True,
            )
            _client = redis_lib.Redis(connection_pool=_pool)
            _client.ping()
            log.info(f"redis.connected url={redis_url[:30]}...")
        except Exception as e:
            if _IS_PRODUCTION:
                raise RuntimeError(
                    f"Redis connection failed in production: {e}. "
                    "Check REDIS_URL and that the Redis service is running."
                ) from e
            log.warning(f"Redis connection failed: {e} — using in-memory fallback (dev/test only)")
            _client = _FakeRedis()

        return _client


# Blocking commands (BLPOP/BRPOP/BLMOVE) hold the connection open server-side
# for up to their own `timeout` argument while waiting for data. If the
# client's socket read timeout is <= that duration, the client can time out
# client-side while the server is still legitimately blocking — a classic
# redis-py footgun that produces spurious "Timeout reading from socket" errors
# on essentially every idle poll. This must stay comfortably larger than the
# longest blocking-command timeout used anywhere in the app (currently
# app.core.event_queue.BLOCK_SECONDS = 5s).
BLOCKING_SOCKET_TIMEOUT = 30

_blocking_pool: redis_lib.ConnectionPool | None = None
_blocking_client = None


def get_redis_blocking() -> "redis_lib.Redis | _FakeRedis":
    """
    Like get_redis(), but backed by a connection pool with a generous socket
    timeout suitable for blocking commands. Use this for BLPOP/BRPOP/BLMOVE;
    use get_redis() for everything else so fast operations still fail fast
    when Redis is genuinely down.

    Falls back to get_redis()'s own in-memory stub when REDIS_URL is unset,
    so dev/test callers share the identical fake store.
    """
    global _blocking_pool, _blocking_client

    if _blocking_client is not None:
        return _blocking_client

    with _client_lock:
        if _blocking_client is not None:
            return _blocking_client

        redis_url = os.environ.get("REDIS_URL", "")
        if not redis_url:
            _blocking_client = get_redis()
            return _blocking_client

        try:
            _blocking_pool = redis_lib.ConnectionPool.from_url(
                redis_url,
                max_connections=6,
                socket_connect_timeout=5,
                socket_timeout=BLOCKING_SOCKET_TIMEOUT,
                retry_on_timeout=True,
                decode_responses=True,
            )
            _blocking_client = redis_lib.Redis(connection_pool=_blocking_pool)
            _blocking_client.ping()
            log.info(f"redis.blocking_connected url={redis_url[:30]}...")
        except Exception as e:
            if _IS_PRODUCTION:
                raise RuntimeError(
                    f"Redis (blocking pool) connection failed in production: {e}. "
                    "Check REDIS_URL and that the Redis service is running."
                ) from e
            log.warning(
                f"Redis blocking-pool connection failed: {e} — using in-memory fallback (dev/test only)"
            )
            _blocking_client = _FakeRedis()

        return _blocking_client


def is_redis_available() -> bool:
    """Returns True if real Redis is connected (not in-memory fallback)."""
    try:
        client = get_redis()
        if isinstance(client, _FakeRedis):
            return False
        client.ping()
        return True
    except Exception:
        return False


def reset_client() -> None:
    """Force-reset the singleton(s) (tests only)."""
    global _pool, _client, _blocking_pool, _blocking_client
    with _client_lock:
        _pool = None
        _client = None
        _blocking_pool = None
        _blocking_client = None


class _FakeRedis:
    """
    In-memory Redis stub for local dev / tests without Redis.
    Data is lost on restart — acceptable for non-production use only.

    FIXED: hset() signature now matches redis-py exactly.
    """

    def __init__(self):
        self._store: dict = {}
        self._expiry: dict = {}
        self._lock = threading.Lock()

    def _is_expired(self, key: str) -> bool:
        import time

        exp = self._expiry.get(key)
        return exp is not None and time.time() > exp

    def _evict(self, key: str):
        """Evict if expired. Must be called inside lock."""
        if self._is_expired(key):
            self._store.pop(key, None)
            self._expiry.pop(key, None)

    def get(self, key: str):
        with self._lock:
            self._evict(key)
            return self._store.get(key)

    def set(self, key: str, value, ex: int = None, nx: bool = False):
        import time

        with self._lock:
            self._evict(key)
            if nx and key in self._store:
                return None
            self._store[key] = str(value)
            if ex:
                self._expiry[key] = time.time() + ex
            return True

    def incr(self, key: str) -> int:
        with self._lock:
            self._evict(key)
            val = int(self._store.get(key, 0)) + 1
            self._store[key] = str(val)
            return val

    def incrby(self, key: str, amount: int) -> int:
        with self._lock:
            self._evict(key)
            val = int(self._store.get(key, 0)) + amount
            self._store[key] = str(val)
            return val

    def expire(self, key: str, seconds: int):
        import time

        with self._lock:
            if key in self._store:
                self._expiry[key] = time.time() + seconds

    def delete(self, *keys):
        with self._lock:
            for k in keys:
                self._store.pop(k, None)
                self._expiry.pop(k, None)

    def exists(self, key: str) -> int:
        with self._lock:
            self._evict(key)
            return 1 if key in self._store else 0

    def lpush(self, key: str, *values):
        with self._lock:
            lst = self._store.get(key, [])
            if not isinstance(lst, list):
                lst = []
            for v in values:
                lst.insert(0, str(v))
            self._store[key] = lst
            return len(lst)

    def lrange(self, key: str, start: int, end: int) -> list:
        with self._lock:
            lst = self._store.get(key, [])
            if not isinstance(lst, list):
                return []
            return lst[start:] if end == -1 else lst[start : end + 1]

    def ltrim(self, key: str, start: int, end: int):
        with self._lock:
            lst = self._store.get(key, [])
            if isinstance(lst, list):
                self._store[key] = lst[start : end + 1]

    def llen(self, key: str) -> int:
        with self._lock:
            lst = self._store.get(key, [])
            return len(lst) if isinstance(lst, list) else 0

    def rpop(self, key: str):
        with self._lock:
            lst = self._store.get(key, [])
            if isinstance(lst, list) and lst:
                return lst.pop()
            return None

    def lrem(self, key: str, count: int, value) -> int:
        with self._lock:
            lst = self._store.get(key, [])
            if not isinstance(lst, list):
                return 0
            removed = 0
            v = str(value)
            n = abs(count) or len(lst)
            out = []
            for item in lst:
                if item == v and removed < n:
                    removed += 1
                    continue
                out.append(item)
            self._store[key] = out
            return removed

    def blmove(
        self,
        first_list: str,
        second_list: str,
        timeout: float = 0,
        src: str = "LEFT",
        dest: str = "RIGHT",
    ):
        # Matches redis-py blmove(first_list, second_list, timeout, src, dest).
        # Non-blocking approximation for tests.
        with self._lock:
            lst = self._store.get(first_list, [])
            if not (isinstance(lst, list) and lst):
                return None
            item = lst.pop() if src.upper() == "RIGHT" else lst.pop(0)
            dlist = self._store.get(second_list, [])
            if not isinstance(dlist, list):
                dlist = []
            if dest.upper() == "LEFT":
                dlist.insert(0, item)
            else:
                dlist.append(item)
            self._store[second_list] = dlist
            return item

    def ping(self) -> bool:
        return True

    def zadd(self, key: str, mapping: dict):
        with self._lock:
            zset = self._store.get(key, {})
            if not isinstance(zset, dict):
                zset = {}
            zset.update(mapping)
            self._store[key] = zset

    def zrange(self, key: str, start: int, end: int, withscores: bool = False):
        with self._lock:
            zset = self._store.get(key, {})
            if not isinstance(zset, dict):
                return []
            sorted_items = sorted(zset.items(), key=lambda x: x[1])
            sliced = sorted_items[start:] if end == -1 else sorted_items[start : end + 1]
            return sliced if withscores else [item[0] for item in sliced]

    def zremrangebyrank(self, key: str, start: int, end: int):
        with self._lock:
            zset = self._store.get(key, {})
            if not isinstance(zset, dict):
                return
            sorted_keys = sorted(zset.items(), key=lambda x: x[1])
            for k, _ in sorted_keys[start : end + 1]:
                zset.pop(k, None)

    def hset(self, name: str, key=None, value=None, mapping=None, items=None):
        """
        Matches redis-py hset signature:
          hset(name, key, value)
          hset(name, mapping={...})
          hset(name, items=[key, val, ...])
        """
        with self._lock:
            h = self._store.get(name, {})
            if not isinstance(h, dict):
                h = {}
            if mapping:
                h.update(mapping)
            if key is not None and value is not None:
                h[str(key)] = str(value)
            if items:
                it = iter(items)
                for k in it:
                    h[str(k)] = str(next(it, ""))
            self._store[name] = h

    def hget(self, key: str, field: str):
        with self._lock:
            h = self._store.get(key, {})
            return h.get(field) if isinstance(h, dict) else None

    def hgetall(self, key: str) -> dict:
        with self._lock:
            h = self._store.get(key, {})
            return h if isinstance(h, dict) else {}
