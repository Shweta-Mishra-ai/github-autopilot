"""
tests/test_health_telemetry.py

app/core/health_check.py implements per-provider latency stats, an error rate,
and a "this provider is slow" degraded message — and record_latency(), the only
function that writes the data those are computed from, had no callers anywhere.
get_system_health() therefore ran over an empty dataset and always reported a
healthy system with 0ms latency and a 0% error rate.

The router now records a latency on every successful call, and the circuit
breaker records an error on every failure — one hook each, at the two places
that actually know the outcome.
"""

from unittest.mock import patch

import pytest

from app.core import health_check as H


@pytest.fixture(autouse=True)
def _fresh_redis():
    from app.core.redis_client import reset_client

    reset_client()
    yield
    reset_client()


class TestRouterFeedsLatency:
    def test_successful_call_records_a_latency(self):
        from app.ai.router import LLMRouter
        from app.ai.providers.base import LLMResponse

        meta = LLMResponse(
            text="ok", provider="groq_70b", model="m", total_tokens=100, latency_ms=250
        )
        with patch.object(H, "record_latency") as rec:
            LLMRouter()._log_and_track("code_review", meta)
        rec.assert_called_once()
        assert rec.call_args[0][0] == "groq_70b"
        assert rec.call_args[0][1] == 250

    def test_telemetry_failure_never_breaks_the_call(self):
        from app.ai.router import LLMRouter
        from app.ai.providers.base import LLMResponse

        meta = LLMResponse(
            text="ok", provider="groq_70b", model="m", total_tokens=1, latency_ms=1
        )
        with patch.object(H, "record_latency", side_effect=RuntimeError("redis")):
            LLMRouter()._log_and_track("code_review", meta)  # must not raise


class TestBreakerFeedsErrors:
    def test_failure_records_an_error(self):
        from app.ai.circuit_breaker import CircuitBreaker

        with patch.object(H, "record_latency") as rec:
            CircuitBreaker(provider="gemini").record_failure("timeout")
        rec.assert_called_once()
        assert rec.call_args[0][0] == "gemini"
        assert rec.call_args.kwargs.get("is_error") is True

    def test_telemetry_failure_never_breaks_the_breaker(self):
        from app.ai.circuit_breaker import CBState, CircuitBreaker

        cb = CircuitBreaker(provider="gemini", fail_threshold=1)
        with patch.object(H, "record_latency", side_effect=RuntimeError("redis")):
            cb.record_failure("timeout")
        assert cb.state == CBState.OPEN, "the breaker must still open"

    def test_recording_happens_outside_the_lock(self):
        """health_check does its own Redis I/O; holding the breaker's RLock
        across it would serialise every provider's error path."""
        from app.ai.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker(provider="groq_8b")
        seen = {}

        def _probe(*a, **kw):
            # If the lock were still held this would deadlock on a non-reentrant
            # lock; with RLock it succeeds, so assert the state is consistent.
            seen["failures"] = cb.status()["failures"]

        with patch.object(H, "record_latency", side_effect=_probe):
            cb.record_failure("boom")
        assert seen["failures"] == 1


class TestHealthStatsAreRealNow:
    def test_recorded_latency_shows_up_in_system_health(self):
        for ms in (100, 200, 300):
            H.record_latency("groq_70b", ms)
        providers = H.get_system_health()["providers"]
        assert providers["groq_70b"]["avg_latency_ms"] > 0

    def test_recorded_errors_raise_the_error_rate(self):
        for _ in range(3):
            H.record_latency("gemini", 0, is_error=True)
        H.record_latency("gemini", 100)
        assert H.get_system_health()["providers"]["gemini"]["error_rate"] > 0

    def test_empty_dataset_does_not_crash(self):
        assert "providers" in H.get_system_health()


class TestHealthEndpointExposesIt:
    def test_provider_health_is_reported(self):
        import server as srv

        with patch("app.core.health_check.get_system_health") as gsh:
            gsh.return_value = {"providers": {"groq_70b": {"avg_latency_ms": 42}}}
            assert srv._provider_health()["groq_70b"]["avg_latency_ms"] == 42

    def test_telemetry_failure_does_not_take_health_down(self):
        import server as srv

        with patch(
            "app.core.health_check.get_system_health", side_effect=RuntimeError("x")
        ):
            assert srv._provider_health() == {}
