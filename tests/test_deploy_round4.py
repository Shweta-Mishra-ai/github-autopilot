"""
tests/test_deploy_round4.py

Round-4 deploy audit (Render free tier, cloud-only providers). Each case was
reproduced against the code first.
"""

from __future__ import annotations

import pytest

from app.ai.circuit_breaker import CircuitBreaker


class TestASpentQuotaIsNotAShortOutage:
    def test_the_providers_own_wait_holds_the_breaker(self):
        cb = CircuitBreaker("groq_70b", fail_threshold=3, recovery_timeout=60)
        cb.record_rate_limit(14_400)
        assert not cb.is_available()
        assert cb.seconds_until_retry() > 14_000

    def test_a_short_wait_is_counted_as_before(self):
        cb = CircuitBreaker("groq_70b", fail_threshold=3, recovery_timeout=60)
        cb.record_rate_limit(20)
        assert cb.is_available(), "one short 429 does not open the breaker"

    def test_a_success_clears_the_hold(self):
        cb = CircuitBreaker("groq_70b", fail_threshold=3, recovery_timeout=60)
        cb.record_rate_limit(14_400)
        cb.record_success()
        assert cb.is_available() and cb.seconds_until_retry() == 0

    def test_users_are_told_it_is_a_quota_with_its_reset(self):
        from app.ai.guarded import degraded_comment

        out = degraded_comment({"_degraded": True, "_reason": "providers_down", "_retry_in": 14_400})
        assert "Quota Reached" in out and "~4h 0m" in out
        assert "try again shortly" not in out.lower()
        short = degraded_comment({"_degraded": True, "_reason": "providers_down", "_retry_in": 60})
        assert "Temporarily Unavailable" in short


class TestHealthCountsOnlyConfiguredProviders:
    """/health said "ok" with Groq's quota spent: the Gemini and OpenRouter
    breakers were closed only because, with no key, they were never called."""

    @pytest.fixture
    def server(self, monkeypatch):
        import server

        for key in ("GEMINI_API_KEY", "OPENROUTER_API_KEY", "OLLAMA_HOST"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("GROQ_API_KEY", "k")
        return server

    def test_an_unconfigured_provider_does_not_count(self, server):
        breakers = {
            "groq_70b": {"state": "open"}, "groq_8b": {"state": "open"},
            "gemini": {"state": "closed"}, "openrouter": {"state": "closed"},
        }
        assert server._any_configured_provider_up(breakers) is False

    def test_a_configured_provider_not_yet_called_counts(self, server, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        assert server._any_configured_provider_up(
            {"groq_70b": {"state": "open"}, "groq_8b": {"state": "open"}}
        ) is True

    def test_nothing_configured_is_not_ok(self, server, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY")
        assert server._any_configured_provider_up({}) is False


class TestAFullRedisIsNotOk:
    """A noeviction Redis at its cap still answers PING; /health said "ok"
    while queued events were stranded."""

    def _health(self, monkeypatch, mem_level="ok", failing=False):
        import server
        from app.core import event_queue

        monkeypatch.setattr(server, "METRICS_TOKEN", "")
        monkeypatch.setattr(server, "_PRODUCTION", False)
        monkeypatch.setattr("app.core.redis_client.redis_memory_status",
                            lambda: {"level": mem_level, "used_pct": 99.0})
        monkeypatch.setattr(server, "_any_configured_provider_up", lambda b: True)
        monkeypatch.setattr(event_queue, "_last_consumer_error",
                            __import__("time").time() if failing else 0.0)
        monkeypatch.delenv("REDIS_URL", raising=False)
        return server.app.test_client().get("/health").get_json()

    def test_memory_at_the_cap_is_degraded(self, monkeypatch):
        assert self._health(monkeypatch, mem_level="warn")["status"] == "degraded"

    def test_a_failing_consumer_is_degraded(self, monkeypatch):
        assert self._health(monkeypatch, failing=True)["status"] == "degraded"

    def test_otherwise_ok(self, monkeypatch):
        assert self._health(monkeypatch)["status"] == "ok"


def test_the_consumer_backs_off(monkeypatch):
    from app.core import event_queue as eq

    waits = []
    calls = {"n": 0}

    def boom(handler):
        calls["n"] += 1
        if calls["n"] > 4:
            eq._stop.set()
        raise RuntimeError("OOM command not allowed when used memory > 'maxmemory'")

    monkeypatch.setattr(eq, "_consume_once", boom)
    monkeypatch.setattr(eq._stop, "wait", lambda d: waits.append(d))
    was_set = eq._stop.is_set()  # another test may have stopped the consumers
    eq._stop.clear()
    try:
        eq._consume_loop(lambda *a: None, 0)
    finally:
        eq._stop.set() if was_set else eq._stop.clear()
    assert waits[:4] == [1, 2, 4, 8]


def test_a_counter_that_lost_its_expiry_gets_it_back(fake_redis):
    """The CI failure counter set its expiry only on the first increment; had
    that one EXPIRE failed, the key lived for ever."""
    from app.handlers.ci import _CI_PATTERN_WINDOW, _track_failure_pattern

    key = "ci_fail:o/r:1:tests"
    fake_redis.set(key, "1")  # as if the first EXPIRE had failed
    assert fake_redis.ttl(key) == -1
    _track_failure_pattern("o/r", 1, "tests", "x")
    assert 0 < fake_redis.ttl(key) <= _CI_PATTERN_WINDOW
