"""
tests/test_redis_client.py — V5
Tests for thread-safe Redis singleton + FakeRedis correctness.

Covers V5 fixes:
  - Double-checked locking race condition
  - FakeRedis.incrby (was completely missing in V4)
  - reset_client() for test isolation
"""

import os
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.core.redis_client as rc


def setup_function():
    rc.reset_client()


class TestSingleton:

    def test_returns_same_instance_every_call(self):
        r1 = rc.get_redis()
        r2 = rc.get_redis()
        r3 = rc.get_redis()
        assert r1 is r2 is r3

    def test_thread_safe_singleton(self):
        """No race: 50 threads all get the identical instance."""
        results = []

        def worker():
            results.append(rc.get_redis())

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len({id(r) for r in results}) == 1, (
            "All threads must receive the same Redis instance"
        )

    def test_reset_clears_singleton(self):
        r1 = rc.get_redis()
        rc.reset_client()
        r2 = rc.get_redis()
        # After reset, a new instance is created
        assert r1 is not r2

    def test_no_redis_url_uses_fake(self):
        with patch.dict(os.environ, {"REDIS_URL": ""}):
            rc.reset_client()
            r = rc.get_redis()
        assert isinstance(r, rc._FakeRedis)

    def test_is_redis_available_false_for_fake(self):
        with patch.dict(os.environ, {"REDIS_URL": ""}):
            rc.reset_client()
            assert rc.is_redis_available() is False

    def test_connection_failure_falls_back_to_fake(self):
        import redis as redis_lib
        with patch.dict(os.environ, {"REDIS_URL": "redis://unreachable:9999/0"}):
            rc.reset_client()
            with patch.object(redis_lib.Redis, "ping", side_effect=Exception("refused")):
                r = rc.get_redis()
        assert isinstance(r, rc._FakeRedis)


class TestFakeRedis:

    def setup_method(self):
        rc.reset_client()
        with patch.dict(os.environ, {"REDIS_URL": ""}):
            self.r = rc.get_redis()
        assert isinstance(self.r, rc._FakeRedis)

    # ── Basic ops ─────────────────────────────────────────────────────────

    def test_set_and_get(self):
        self.r.set("k", "v")
        assert self.r.get("k") == "v"

    def test_get_missing_key_returns_none(self):
        assert self.r.get("nonexistent") is None

    def test_set_nx_only_if_absent(self):
        self.r.set("k", "first")
        self.r.set("k", "second", nx=True)
        assert self.r.get("k") == "first"

    def test_set_nx_sets_if_absent(self):
        self.r.set("newkey", "value", nx=True)
        assert self.r.get("newkey") == "value"

    def test_delete(self):
        self.r.set("k", "v")
        self.r.delete("k")
        assert self.r.get("k") is None

    def test_delete_multiple_keys(self):
        self.r.set("a", "1")
        self.r.set("b", "2")
        self.r.delete("a", "b")
        assert self.r.get("a") is None
        assert self.r.get("b") is None

    def test_exists_present(self):
        self.r.set("k", "v")
        assert self.r.exists("k") == 1

    def test_exists_absent(self):
        assert self.r.exists("missing") == 0

    # ── Counters ──────────────────────────────────────────────────────────

    def test_incr_starts_at_one(self):
        assert self.r.incr("counter") == 1

    def test_incr_accumulates(self):
        self.r.incr("counter")
        self.r.incr("counter")
        assert self.r.incr("counter") == 3

    def test_incrby_correct_amount(self):
        """V5 FIX: incrby was completely missing — caused AttributeError."""
        self.r.incrby("tokens", 1500)
        self.r.incrby("tokens", 500)
        assert self.r.get("tokens") == "2000"

    def test_incrby_from_zero(self):
        result = self.r.incrby("fresh_key", 100)
        assert result == 100

    def test_incrby_large_values(self):
        self.r.incrby("big", 1_000_000)
        self.r.incrby("big", 2_000_000)
        assert self.r.get("big") == "3000000"

    def test_incr_and_incrby_compatible(self):
        self.r.incr("mixed")         # +1 = 1
        self.r.incrby("mixed", 99)   # +99 = 100
        self.r.incr("mixed")         # +1 = 101
        assert self.r.get("mixed") == "101"

    # ── Expiry ────────────────────────────────────────────────────────────

    def test_set_with_ex_expires(self):
        import time
        self.r.set("temp", "val", ex=1)
        assert self.r.get("temp") == "val"
        time.sleep(1.1)
        assert self.r.get("temp") is None

    def test_expire_sets_ttl(self):
        import time
        self.r.set("k", "v")
        self.r.expire("k", 1)
        time.sleep(1.1)
        assert self.r.get("k") is None

    def test_key_without_expire_persists(self):
        self.r.set("permanent", "yes")
        assert self.r.get("permanent") == "yes"

    # ── List ops ──────────────────────────────────────────────────────────

    def test_lpush_and_lrange(self):
        self.r.lpush("mylist", "a", "b", "c")
        result = self.r.lrange("mylist", 0, -1)
        assert len(result) == 3

    def test_lpush_newest_first(self):
        self.r.lpush("log", "first")
        self.r.lpush("log", "second")
        result = self.r.lrange("log", 0, 0)
        assert result[0] == "second"

    def test_ltrim(self):
        for i in range(10):
            self.r.lpush("list", str(i))
        self.r.ltrim("list", 0, 4)
        assert len(self.r.lrange("list", 0, -1)) == 5

    def test_lrange_empty_list(self):
        assert self.r.lrange("empty", 0, -1) == []

    # ── Hash ops ──────────────────────────────────────────────────────────

    def test_hset_and_hget(self):
        self.r.hset("myhash", mapping={"field": "value"})
        assert self.r.hget("myhash", "field") == "value"

    def test_hgetall(self):
        self.r.hset("h", mapping={"a": "1", "b": "2"})
        result = self.r.hgetall("h")
        assert result == {"a": "1", "b": "2"}

    def test_hget_missing_field(self):
        self.r.hset("h", mapping={"a": "1"})
        assert self.r.hget("h", "nonexistent") is None

    # ── Thread safety ─────────────────────────────────────────────────────

    def test_concurrent_incr_correct_total(self):
        """100 threads each calling incr once → total must be exactly 100."""
        errors = []

        def worker():
            try:
                self.r.incr("shared_counter")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        val = int(self.r.get("shared_counter"))
        assert val == 100, f"Expected 100, got {val}"

    def test_concurrent_incrby_correct_total(self):
        """50 threads each adding 1000 → total must be exactly 50000."""
        errors = []

        def worker():
            try:
                self.r.incrby("token_counter", 1000)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        val = int(self.r.get("token_counter"))
        assert val == 50_000, f"Expected 50000, got {val}"
