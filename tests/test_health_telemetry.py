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


@pytest.fixture
def registered(request):
    """Make sure the provider under test exists in the breaker registry.

    get_system_health()["providers"] is keyed by the circuit-breaker registry,
    and that registry is lazily populated — a breaker exists only once
    get_breaker(name) has been called for it. These tests asserted on
    providers["groq_70b"] without ever registering it, so they passed only
    because some earlier test in the session happened to touch that provider.

    tests/test_ollama_local.py clears the registry around every test, which is
    correct hygiene for that file and left this one raising KeyError whenever
    the ordering put it afterwards. A test that depends on global state some
    other file established is a test that reports on the ordering.
    """
    from app.ai.circuit_breaker import get_breaker

    return get_breaker


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
    def test_recorded_latency_shows_up_in_system_health(self, registered):
        registered("groq_70b")
        for ms in (100, 200, 300):
            H.record_latency("groq_70b", ms)
        providers = H.get_system_health()["providers"]
        assert providers["groq_70b"]["avg_latency_ms"] > 0

    def test_recorded_errors_raise_the_error_rate(self, registered):
        registered("gemini")
        for _ in range(3):
            H.record_latency("gemini", 0, is_error=True)
        H.record_latency("gemini", 100)
        assert H.get_system_health()["providers"]["gemini"]["error_rate"] > 0

    def test_a_provider_with_no_breaker_is_simply_absent(self, registered):
        """Not an error — the health report describes providers this process
        has actually used. Pinned because the two tests above spent a while
        depending on the opposite by accident."""
        from app.ai.circuit_breaker import _breakers

        _breakers.pop("openrouter", None)
        H.record_latency("openrouter", 100)
        assert "openrouter" not in H.get_system_health()["providers"]

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


class TestHealthReportsOnlyWhatWasMeasured:
    """Round-3 audit F10: /health counters that could never move and checks
    that read "ok" with no data behind them."""

    def _get(self):
        import server

        headers = (
            {"Authorization": f"Bearer {server.METRICS_TOKEN}"} if server.METRICS_TOKEN else {}
        )
        return server.app.test_client().get("/health", headers=headers).get_json()

    def test_github_api_is_unknown_before_any_call(self):
        from app.github import rate_limit

        with patch.object(rate_limit, "_state", {}):
            assert self._get()["checks"]["github_api"].startswith("unknown")

    def test_github_api_reads_the_measured_remaining(self):
        import time

        from app.github import rate_limit

        state = {"k": {"key": "k", "remaining": 10, "reset_at": time.time() + 600}}
        with patch.object(rate_limit, "_state", state):
            body = self._get()
        assert body["checks"]["github_api"] == "rate_limited"
        assert body["status"] != "ok"

    def test_a_secondary_rate_limit_is_counted(self):
        import requests

        from app.core.metrics import metrics
        from app.github import client

        before = metrics.get("github.secondary_rate_limited")
        resp = requests.models.Response()
        resp.status_code = 403
        resp._content = b'{"message": "You have exceeded a secondary rate limit"}'
        with pytest.raises(client.GitHubSecondaryRateLimitError):
            client._handle_response(resp, "GET", "/repos/o/r")
        assert metrics.get("github.secondary_rate_limited") == before + 1
        assert self._get()["metrics"]["secondary_rate_limited"] == before + 1

    def test_unreadable_latency_is_not_zero(self, registered):
        registered("gemini")
        with patch("app.core.redis_client.get_redis", side_effect=ConnectionError("down")):
            p = H.get_system_health()["providers"]["gemini"]
        assert p["avg_latency_ms"] is None and p["error_rate"] is None

    def test_the_dead_degraded_banner_is_gone(self):
        assert not hasattr(H, "get_degraded_message")
