"""
Redis Client - app/core/redis_client.py
V4: Connection pool singleton.
Prevents creating new TCP connection on every Redis operation.
All modules import get_redis() from here — never create their own connections.
"""

import os
import logging
import redis as redis_lib

log = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "")

_pool: redis_lib.ConnectionPool | None = None
_client: redis_lib.Redis | None = None


def get_redis() -> redis_lib.Redis:
    """
    Returns a Redis client backed by a shared connection pool.
    Thread-safe. Creates pool once, reuses on every call.
    Falls back to a no-op client if REDIS_URL not set (local dev without Redis).
    """
    global _pool, _client

    if _client is not None:
        return _client

    if not REDIS_URL:
        log.warning("REDIS_URL not set — using in-memory fallback (no persistence)")
        _client = _FakeRedis()
        return _client

    try:
        _pool = redis_lib.ConnectionPool.from_url(
            REDIS_URL,
            max_connections=10,
            socket_connect_timeout=5,
            socket_timeout=5,
            retry_on_timeout=True,
            decode_responses=True,  # Always return strings, not bytes
        )
        _client = redis_lib.Redis(connection_pool=_pool)
        _client.ping()  # Verify connection works at startup
        log.info(f"redis.connected url={REDIS_URL[:30]}...")
    except Exception as e:
        log.warning(f"Redis connection failed: {e} — using in-memory fallback")
        _client = _FakeRedis()

    return _client


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


class _FakeRedis:
    """
    In-memory Redis stub for local dev without Redis.
    Supports only the methods we actually use.
    Data is lost on restart — acceptable for dev only.
    """

    def __init__(self):
        self._store: dict = {}
        self._expiry: dict = {}
        import threading

        self._lock = threading.Lock()

    def _is_expired(self, key: str) -> bool:
        import time

        exp = self._expiry.get(key)
        return exp is not None and time.time() > exp

    def get(self, key: str):
        with self._lock:
            if self._is_expired(key):
                self._store.pop(key, None)
                self._expiry.pop(key, None)
                return None
            return self._store.get(key)

    def set(self, key: str, value, ex: int = None, nx: bool = False):
        import time

        with self._lock:
            if nx and key in self._store and not self._is_expired(key):
                return None  # Key exists, nx=True means don't overwrite
            self._store[key] = str(value)
            if ex:
                self._expiry[key] = time.time() + ex
            return True

    def incr(self, key: str) -> int:
        with self._lock:
            val = int(self._store.get(key, 0)) + 1
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
            if self._is_expired(key):
                return 0
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
            if end == -1:
                return lst[start:]
            return lst[start : end + 1]

    def ltrim(self, key: str, start: int, end: int):
        with self._lock:
            lst = self._store.get(key, [])
            if isinstance(lst, list):
                self._store[key] = lst[start : end + 1]

    def ping(self):
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
            if end == -1:
                sliced = sorted_items[start:]
            else:
                sliced = sorted_items[start : end + 1]
            if withscores:
                return sliced
            return [item[0] for item in sliced]

    def zremrangebyrank(self, key: str, start: int, end: int):
        with self._lock:
            zset = self._store.get(key, {})
            if not isinstance(zset, dict):
                return
            sorted_keys = sorted(zset.items(), key=lambda x: x[1])
            to_remove = sorted_keys[start : end + 1]
            for k, _ in to_remove:
                zset.pop(k, None)

    def hset(self, key: str, mapping: dict = None, **kwargs):
        with self._lock:
            h = self._store.get(key, {})
            if not isinstance(h, dict):
                h = {}
            if mapping:
                h.update(mapping)
            h.update(kwargs)
            self._store[key] = h

    def hget(self, key: str, field: str):
        with self._lock:
            h = self._store.get(key, {})
            if not isinstance(h, dict):
                return None
            return h.get(field)

    def hgetall(self, key: str) -> dict:
        with self._lock:
            h = self._store.get(key, {})
            return h if isinstance(h, dict) else {}
