"""
tests/test_router.py — V5
Router tests updated for:
  - safe_ask() method (new in V5)
  - _check_budget_alert() (new in V5)
  - Cost tracking uses incrby not incr
  - AllProvidersDown handled gracefully
"""

import os
import sys
from unittest.mock import patch, MagicMock, call

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GROQ_API_KEY", "test")
os.environ.setdefault("REDIS_URL", "")

import app.core.redis_client as rc


def setup_function():
    rc.reset_client()


class TestLLMRouter:

    def _make_router(self):
        from app.ai.router import LLMRouter
        return LLMRouter()

    def _mock_provider_response(self, text="response text", tokens=150, cost=0.0002):
        from app.ai.providers.base import LLMResponse
        return LLMResponse(
            text=text,
            provider="groq",
            model="llama-3.3-70b-versatile",
            total_tokens=tokens,
            prompt_tokens=100,
            completion_tokens=tokens - 100,
            cost_usd=cost,
        )

    def test_router_has_safe_ask_method(self):
        """V5: safe_ask must exist and not raise on AllProvidersDown."""
        router = self._make_router()
        assert hasattr(router, "safe_ask"), "safe_ask method must exist in V5"

    def test_safe_ask_returns_degraded_dict_when_all_down(self):
        """V5 FIX: AllProvidersDown must return degraded dict, not raise."""
        from app.ai.circuit_breaker import AllProvidersDown
        router = self._make_router()
        with patch.object(router, "ask", side_effect=AllProvidersDown(retry_in_seconds=30)):
            result, meta = router.safe_ask("sys", "usr", task="test")
        assert result.get("_providers_down") is True
        assert meta is None

    def test_safe_ask_passes_through_on_success(self):
        """safe_ask returns normal (dict, meta) on success."""
        router = self._make_router()
        expected_resp = self._mock_provider_response()
        with patch.object(router, "ask", return_value=({"result": "ok"}, expected_resp)):
            result, meta = router.safe_ask("sys", "usr", task="test")
        assert result == {"result": "ok"}
        assert meta is expected_resp

    def test_router_has_budget_alert_method(self):
        router = self._make_router()
        assert hasattr(router, "_check_budget_alert"), "_check_budget_alert must exist"

    def test_budget_alert_fires_at_80_percent(self):
        """V5: warning must be logged when token usage ≥ 80% of daily limit."""
        import logging
        router = self._make_router()
        r = rc.get_redis()
        import datetime
        today = datetime.date.today().isoformat()

        # Simulate 80% of daily limit
        from app.ai.router import DAILY_LIMITS
        limit = DAILY_LIMITS.get("groq_70b", {}).get("tokens", 100_000)
        r.set(f"llm:tokens:groq_70b:{today}", str(int(limit * 0.85)))

        with patch("app.ai.router.log") as mock_log:
            router._check_budget_alert(r, "groq_70b", today)
        mock_log.warning.assert_called()
        warning_msg = str(mock_log.warning.call_args)
        assert "budget_alert" in warning_msg or "80" in warning_msg or "pct" in warning_msg

    def test_budget_alert_silent_below_threshold(self):
        """No warning logged at 50% usage."""
        import logging
        router = self._make_router()
        r = rc.get_redis()
        import datetime
        today = datetime.date.today().isoformat()
        from app.ai.router import DAILY_LIMITS
        limit = DAILY_LIMITS.get("groq_70b", {}).get("tokens", 100_000)
        r.set(f"llm:tokens:groq_70b:{today}", str(int(limit * 0.50)))
        with patch("app.ai.router.log") as mock_log:
            router._check_budget_alert(r, "groq_70b", today)
        mock_log.warning.assert_not_called()

    def test_log_and_track_uses_incrby_for_cost(self):
        """V5 FIX: cost tracking must use incrby(cost_mc) not incr(1)."""
        import inspect
        from app.ai.router import LLMRouter
        src = inspect.getsource(LLMRouter._log_and_track)
        assert "incrby(cost_key" in src, (
            "Cost tracking must use r.incrby(cost_key, cost_mc). "
            "V4 used r.incr() which always added 1 micro-cent."
        )

    def test_ask_calls_provider(self):
        router = self._make_router()
        mock_resp = self._mock_provider_response("AI answer", 200, 0.0003)
        with patch.object(router, "_call_provider", return_value=mock_resp):
            result, meta = router.ask("system prompt", "user query", task="test")
        assert meta is not None

    def test_daily_limits_defined(self):
        from app.ai.router import DAILY_LIMITS
        assert isinstance(DAILY_LIMITS, dict)
        assert "groq_70b" in DAILY_LIMITS
        assert "tokens" in DAILY_LIMITS["groq_70b"]


class TestAllProvidersDown:

    def test_all_providers_down_is_importable(self):
        from app.ai.circuit_breaker import AllProvidersDown
        exc = AllProvidersDown(retry_in_seconds=60)
        assert exc.retry_in_seconds == 60

    def test_all_providers_down_is_exception(self):
        from app.ai.circuit_breaker import AllProvidersDown
        assert issubclass(AllProvidersDown, Exception)
