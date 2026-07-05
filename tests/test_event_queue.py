"""
tests/test_event_queue.py — Durable Redis event queue.

Covers: enqueue paths (ok / full / unavailable / too-large), FIFO consume,
at-least-once crash recovery, dead-lettering, graceful degradation, stats.
"""

import json

import pytest

import app.core.event_queue as eq
from app.core.redis_client import _FakeRedis


@pytest.fixture()
def fake_redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(eq, "get_redis", lambda: r)
    monkeypatch.setattr(eq, "get_redis_blocking", lambda: r)
    monkeypatch.setattr(eq, "is_redis_available", lambda: True)
    return r


def _envelope(r, idx=0):
    raw = r.lrange(eq.PENDING_KEY, 0, -1)[idx]
    return json.loads(raw)


# ── enqueue ───────────────────────────────────────────────────────────────────


class TestEnqueue:
    def test_ok_puts_envelope_in_pending(self, fake_redis):
        res = eq.enqueue("push", {"a": 1}, "o/r", "deliv-1")
        assert res == eq.EnqueueResult.OK
        env = _envelope(fake_redis)
        assert env["event"] == "push"
        assert env["repo"] == "o/r"
        assert env["id"] == "deliv-1"
        assert env["attempts"] == 0
        assert env["payload"] == {"a": 1}

    def test_full_queue_rejected(self, fake_redis, monkeypatch):
        monkeypatch.setattr(eq, "MAX_QUEUE_LEN", 2)
        assert eq.enqueue("push", {}, "o/r") == eq.EnqueueResult.OK
        assert eq.enqueue("push", {}, "o/r") == eq.EnqueueResult.OK
        assert eq.enqueue("push", {}, "o/r") == eq.EnqueueResult.FULL
        assert fake_redis.llen(eq.PENDING_KEY) == 2

    def test_redis_unavailable(self, monkeypatch):
        monkeypatch.setattr(eq, "is_redis_available", lambda: False)
        assert eq.enqueue("push", {}, "o/r") == eq.EnqueueResult.UNAVAILABLE

    def test_oversized_payload_rejected(self, fake_redis, monkeypatch):
        monkeypatch.setattr(eq, "MAX_ENVELOPE_BYTES", 100)
        res = eq.enqueue("push", {"blob": "x" * 500}, "o/r")
        assert res == eq.EnqueueResult.TOO_LARGE
        assert fake_redis.llen(eq.PENDING_KEY) == 0

    def test_redis_error_degrades_to_unavailable(self, monkeypatch):
        class Boom:
            def llen(self, *a):
                raise ConnectionError("redis gone")

        monkeypatch.setattr(eq, "is_redis_available", lambda: True)
        monkeypatch.setattr(eq, "get_redis", lambda: Boom())
        assert eq.enqueue("push", {}, "o/r") == eq.EnqueueResult.UNAVAILABLE


# ── consume ───────────────────────────────────────────────────────────────────


class TestConsume:
    def test_consume_calls_handler_and_clears_processing(self, fake_redis):
        eq.enqueue("issues", {"n": 7}, "o/r", "d1")
        seen = []
        processed = eq._consume_once(lambda ev, pl, repo: seen.append((ev, pl, repo)))
        assert processed is True
        assert seen == [("issues", {"n": 7}, "o/r")]
        assert fake_redis.llen(eq.PENDING_KEY) == 0
        assert fake_redis.llen(eq.PROCESSING_KEY) == 0

    def test_empty_queue_returns_false(self, fake_redis):
        assert eq._consume_once(lambda *a: None) is False

    def test_fifo_ordering(self, fake_redis):
        eq.enqueue("push", {}, "o/r", "first")
        eq.enqueue("push", {}, "o/r", "second")
        order = []
        eq._consume_once(lambda ev, pl, repo: order.append("first"))
        # verify by draining ids directly
        env = _envelope(fake_redis)
        assert env["id"] == "second"  # first was consumed first (FIFO)

    def test_handler_exception_still_clears_processing(self, fake_redis):
        eq.enqueue("push", {}, "o/r", "d1")

        def bad_handler(ev, pl, repo):
            raise RuntimeError("handler blew up")

        with pytest.raises(RuntimeError):
            eq._consume_once(bad_handler)
        # not stuck in processing forever
        assert fake_redis.llen(eq.PROCESSING_KEY) == 0


# ── crash recovery ────────────────────────────────────────────────────────────


class TestRecovery:
    def _stranded(self, r, attempts=0, raw=None):
        env = raw or json.dumps(
            {"id": "d1", "event": "push", "repo": "o/r", "payload": {}, "attempts": attempts}
        )
        r.lpush(eq.PROCESSING_KEY, env)

    def test_stranded_event_requeued_with_attempt_bump(self, fake_redis):
        self._stranded(fake_redis, attempts=0)
        assert eq.recover_stale() == 1
        assert fake_redis.llen(eq.PROCESSING_KEY) == 0
        env = _envelope(fake_redis)
        assert env["attempts"] == 1

    def test_exhausted_attempts_dead_lettered(self, fake_redis):
        self._stranded(fake_redis, attempts=1)  # +1 == MAX_ATTEMPTS
        assert eq.recover_stale() == 0
        assert fake_redis.llen(eq.PENDING_KEY) == 0
        assert fake_redis.llen(eq.DEAD_KEY) == 1

    def test_corrupt_envelope_dead_lettered(self, fake_redis):
        self._stranded(fake_redis, raw="{not json")
        assert eq.recover_stale() == 0
        assert fake_redis.llen(eq.DEAD_KEY) == 1

    def test_dead_letter_list_trimmed(self, fake_redis, monkeypatch):
        monkeypatch.setattr(eq, "DEAD_MAX", 3)
        for _ in range(5):
            self._stranded(fake_redis, attempts=1)
        eq.recover_stale()
        assert fake_redis.llen(eq.DEAD_KEY) <= 3

    def test_no_redis_recovers_nothing(self, monkeypatch):
        monkeypatch.setattr(eq, "is_redis_available", lambda: False)
        assert eq.recover_stale() == 0


# ── stats & lifecycle ─────────────────────────────────────────────────────────


class TestStatsAndLifecycle:
    def test_stats_redis_mode(self, fake_redis):
        eq.enqueue("push", {}, "o/r")
        stats = eq.queue_stats()
        assert stats["mode"] == "redis"
        assert stats["pending"] == 1
        assert stats["dead"] == 0

    def test_stats_fallback_mode(self, monkeypatch):
        monkeypatch.setattr(eq, "is_redis_available", lambda: False)
        assert eq.queue_stats()["mode"] == "threadpool-fallback"

    def test_start_consumers_skipped_without_redis(self, monkeypatch):
        monkeypatch.setattr(eq, "is_redis_available", lambda: False)
        assert eq.start_consumers(lambda *a: None) == 0

    def test_start_consumers_disabled_by_env(self, monkeypatch):
        monkeypatch.setattr(eq, "CONSUMER_COUNT", 0)
        assert eq.start_consumers(lambda *a: None) == 0

    def test_start_and_stop_consumers(self, fake_redis, monkeypatch):
        monkeypatch.setattr(eq, "CONSUMER_COUNT", 1)
        try:
            started = eq.start_consumers(lambda *a: None)
            assert started == 1
            # idempotent — second call doesn't double-start
            assert eq.start_consumers(lambda *a: None) == 1
        finally:
            eq.stop_consumers(timeout=6.0)
        assert eq.queue_stats()["consumers"] == 0
