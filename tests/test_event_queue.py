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


# ── outcomes (2026-10-08 audit) ───────────────────────────────────────────────


class TestHandlerOutcomes:
    """The envelope was removed whatever the handler did, so a transient
    GitHub 5xx or rate limit dropped the event for good."""

    def test_retry_requeues_once_then_dead_letters(self, fake_redis, monkeypatch):
        monkeypatch.setattr(eq, "RETRY_DELAY_SECONDS", 0)
        eq.enqueue("push", {}, "o/r", "d1")
        assert eq._consume_once(lambda *a: eq.HandlerOutcome.RETRY) is True
        assert fake_redis.llen(eq.PENDING_KEY) == 1, "requeued for one retry"
        assert eq._consume_once(lambda *a: eq.HandlerOutcome.RETRY) is True
        assert fake_redis.llen(eq.PENDING_KEY) == 0
        assert fake_redis.llen(eq.DEAD_KEY) == 1
        assert fake_redis.llen(eq.PROCESSING_KEY) == 0

    def test_failed_goes_straight_to_dead_letter(self, fake_redis):
        eq.enqueue("push", {}, "o/r", "d1")
        eq._consume_once(lambda *a: eq.HandlerOutcome.FAILED)
        assert fake_redis.llen(eq.DEAD_KEY) == 1
        assert fake_redis.llen(eq.PENDING_KEY) == 0

    def test_ok_and_none_are_consumed(self, fake_redis):
        eq.enqueue("push", {}, "o/r", "d1")
        eq.enqueue("push", {}, "o/r", "d2")
        eq._consume_once(lambda *a: eq.HandlerOutcome.OK)
        eq._consume_once(lambda *a: None)
        assert fake_redis.llen(eq.DEAD_KEY) == 0
        assert fake_redis.llen(eq.PENDING_KEY) == 0


class TestRunHandlerClassifiesFailures:
    def _run(self, exc):
        from unittest.mock import patch

        import server

        with patch("app.handlers.push.handle", side_effect=exc):
            return server._run_handler("push", {}, "o/r")

    def test_github_5xx_is_transient(self):
        from app.github.client import GitHubError

        assert self._run(GitHubError("boom", 502)) == eq.HandlerOutcome.RETRY

    def test_rate_limit_exhausted_is_transient(self):
        from app.github.rate_limit import GitHubRateLimitExhausted

        assert self._run(GitHubRateLimitExhausted("x")) == eq.HandlerOutcome.RETRY

    def test_secondary_rate_limit_is_transient(self):
        from app.github.client import GitHubSecondaryRateLimitError

        exc = GitHubSecondaryRateLimitError.__new__(GitHubSecondaryRateLimitError)
        GitHubError_init = Exception.__init__
        GitHubError_init(exc, "secondary")
        exc.status_code = 403
        exc.retry_after = 60
        assert self._run(exc) == eq.HandlerOutcome.RETRY

    def test_a_bug_is_failed_not_retried(self):
        assert self._run(KeyError("x")) == eq.HandlerOutcome.FAILED

    def test_a_404_is_not_transient(self):
        from app.github.client import GitHubError

        assert self._run(GitHubError("nope", 404)) == eq.HandlerOutcome.FAILED

    def test_errors_are_counted_where_health_reads_them(self):
        from app.core.metrics import metrics

        before = metrics.get("events.error")
        self._run(KeyError("x"))
        assert metrics.get("events.error") == before + 1


class TestA503ReleasesTheDedupKey:
    """The key was set before enqueue; on 503 it stayed, and GitHub's (manual)
    redelivery with the same delivery id was answered 'duplicate — skipped'."""

    def test_redelivery_after_a_full_queue_is_processed(self, fake_redis):
        from app.core import idempotency

        fp = idempotency.make_fingerprint("delivery-1", "push", {"repository": {"full_name": "o/r"}})
        assert idempotency.is_duplicate(fp) is False
        idempotency.forget(fp)
        assert idempotency.is_duplicate(fp) is False, "the redelivery must run"
        assert idempotency.is_duplicate(fp) is True


class TestInstallationRegistry:
    """Installation events have no repository: 'unknown' was registered as one,
    and uninstalls were never forgotten (2026-10-08 audit)."""

    def _run(self, event, payload):
        from unittest.mock import patch

        import server

        with patch("app.core.installations.remember_installation") as remember, \
             patch("app.core.installations.forget_installation") as forget, \
             patch("app.core.installations.touch"):
            server._track_installations(event, payload, payload.get("repository", {}).get("full_name", "unknown"))
        return remember, forget

    def test_an_installation_event_never_registers_unknown(self):
        remember, _ = self._run("installation", {"action": "new_permissions_accepted", "installation": {"id": 9}})
        remember.assert_not_called()

    def test_uninstall_forgets_its_repositories(self):
        _, forget = self._run("installation", {
            "action": "deleted", "installation": {"id": 9},
            "repositories": [{"full_name": "o/a"}, {"full_name": "o/b"}],
        })
        assert [c.args[0] for c in forget.call_args_list] == ["o/a", "o/b"]

    def test_repositories_added_and_removed(self):
        remember, forget = self._run("installation_repositories", {
            "action": "added", "installation": {"id": 9},
            "repositories_added": [{"full_name": "o/new"}],
            "repositories_removed": [{"full_name": "o/old"}],
        })
        remember.assert_called_once_with("o/new", 9)
        forget.assert_called_once_with("o/old")

    def test_an_ordinary_event_registers_its_repo(self):
        remember, _ = self._run("push", {"repository": {"full_name": "o/r"}, "installation": {"id": 9}})
        remember.assert_called_once_with("o/r", 9)
