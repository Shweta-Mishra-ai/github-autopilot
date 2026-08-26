"""
tests/test_providers.py — V5
LLM provider tests. Updated for V5:
  - Token tracking uses incrby (not incr)
  - Retry-After parsed correctly from rate limit response
  - Circuit breaker integration
"""

import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GROQ_API_KEY", "test-groq-key-not-real")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key-not-real")
os.environ.setdefault("REDIS_URL", "")

import app.core.redis_client as rc


def setup_function():
    rc.reset_client()


class TestGroqProvider:

    def _make_response(self, text="ok response", prompt_tok=100, completion_tok=50):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": text}}],
            "usage": {
                "prompt_tokens": prompt_tok,
                "completion_tokens": completion_tok,
                "total_tokens": prompt_tok + completion_tok,
            },
        }
        return mock_resp

    def test_successful_call_returns_text(self):
        from app.ai.providers.groq import GroqProvider
        provider = GroqProvider()
        with patch("app.ai.providers.groq.http_requests.post",
                   return_value=self._make_response("hello")), \
             patch("app.ai.circuit_breaker.get_breaker") as mock_breaker:
                mock_breaker.return_value.is_available.return_value = True
                result = provider.call_raw("sys", "usr", 500, 0.2, 30)
        assert result.text == "hello"
        assert result.error is None

    def test_token_tracking_uses_incrby(self):
        """
        V5 CRITICAL FIX: _track() must call incrby(tok_key, total_tokens)
        not incr(tok_key). V4 always incremented by 1 making /budget useless.
        """
        rc.reset_client()
        r = rc.get_redis()

        from app.ai.providers.groq import GroqProvider
        provider = GroqProvider()

        import inspect
        src = inspect.getsource(provider._track)
        assert "incrby" in src, (
            "CRITICAL: _track() must use r.incrby(tok_key, total_tokens) "
            "not r.incr(tok_key). V4 always added 1 regardless of token count."
        )

    def test_token_tracking_accumulates_correctly(self):
        rc.reset_client()
        with patch.dict(os.environ, {"REDIS_URL": ""}):
            r = rc.get_redis()

        from app.ai.providers.groq import GroqProvider
        provider = GroqProvider()

        import datetime
        today = datetime.date.today().isoformat()
        tok_key = f"llm:tokens:{provider.provider_key}:{today}"

        with patch("app.ai.providers.groq.get_redis", return_value=r):
            provider._track(1500)  # First call: 1500 tokens
            provider._track(500)   # Second call: 500 tokens
            # Total must be 2000, not 2 (which incr(1) would give)
            assert r.get(tok_key) == "2000", (
                f"Token counter must be 2000 (sum of actual tokens), "
                f"got {r.get(tok_key)}. V4 bug would give '2'."
            )

    def test_request_counter_uses_incr_one(self):
        """Request counter (not token counter) must increment by 1 per call."""
        rc.reset_client()
        with patch.dict(os.environ, {"REDIS_URL": ""}):
            r = rc.get_redis()

        from app.ai.providers.groq import GroqProvider
        provider = GroqProvider()

        import datetime
        today = datetime.date.today().isoformat()
        req_key = f"llm:requests:{provider.provider_key}:{today}"

        with patch("app.ai.providers.groq.get_redis", return_value=r):
            provider._track(1500)
            provider._track(2000)
            provider._track(750)
            assert r.get(req_key) == "3"  # 3 calls, each adds 1

    def test_rate_limit_returns_retry_after(self):
        from app.ai.providers.groq import GroqProvider
        provider = GroqProvider()
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.headers = {"Retry-After": "45"}
        with patch("app.ai.providers.groq.http_requests.post", return_value=mock_resp), \
             patch("app.ai.circuit_breaker.get_breaker") as mock_breaker:
                mock_breaker.return_value.is_available.return_value = True
                result = provider.call_raw("sys", "usr", 500, 0.2, 30)
        assert "RATE_LIMIT:45" in result.error
        assert result.text == ""

    def test_circuit_open_short_circuits(self):
        """When circuit is open, no HTTP call should be made."""
        from app.ai.providers.groq import GroqProvider
        provider = GroqProvider()
        with patch("app.ai.circuit_breaker.get_breaker") as mock_breaker:
            mock_breaker.return_value.is_available.return_value = False
            with patch("app.ai.providers.groq.http_requests.post") as mock_post:
                result = provider.call_raw("sys", "usr", 500, 0.2, 30)
        mock_post.assert_not_called()
        assert "Circuit OPEN" in result.error

    def test_missing_api_key_returns_error(self):
        from app.ai.providers.groq import GroqProvider
        provider = GroqProvider()
        with patch.dict(os.environ, {"GROQ_API_KEY": ""}), \
             patch("app.ai.circuit_breaker.get_breaker") as mock_breaker, \
             patch("app.ai.providers.groq.GROQ_API_KEY", ""):
            mock_breaker.return_value.is_available.return_value = True
            result = provider.call_raw("sys", "usr", 500, 0.2, 30)
        assert "GROQ_API_KEY" in result.error

    def test_timeout_records_circuit_failure(self):
        import requests as req_lib
        from app.ai.providers.groq import GroqProvider
        provider = GroqProvider()
        with patch("app.ai.providers.groq.http_requests.post",
                   side_effect=req_lib.exceptions.Timeout()), \
             patch("app.ai.circuit_breaker.get_breaker") as mock_breaker:
                mock_breaker.return_value.is_available.return_value = True
                result = provider.call_raw("sys", "usr", 500, 0.2, 30)
        mock_breaker.return_value.record_failure.assert_called()
        assert result.error is not None

    def test_500_server_error_opens_circuit(self):
        from app.ai.providers.groq import GroqProvider
        provider = GroqProvider()
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        with patch("app.ai.providers.groq.http_requests.post", return_value=mock_resp), \
             patch("app.ai.circuit_breaker.get_breaker") as mock_breaker:
                mock_breaker.return_value.is_available.return_value = True
                result = provider.call_raw("sys", "usr", 500, 0.2, 30)
        mock_breaker.return_value.record_failure.assert_called()

    def test_track_skips_zero_tokens(self):
        """_track with 0 tokens should be a no-op (no Redis writes)."""
        rc.reset_client()
        with patch.dict(os.environ, {"REDIS_URL": ""}):
            r = rc.get_redis()
        from app.ai.providers.groq import GroqProvider
        provider = GroqProvider()
        with patch("app.ai.providers.groq.get_redis", return_value=r):
            provider._track(0)
        import datetime
        today = datetime.date.today().isoformat()
        assert r.get(f"llm:tokens:{provider.provider_key}:{today}") is None

    def test_llm_response_fields(self):
        from app.ai.providers.groq import GroqProvider
        from app.ai.providers.base import LLMResponse
        provider = GroqProvider()
        resp = self._make_response("test output", 200, 100)
        with patch("app.ai.providers.groq.http_requests.post", return_value=resp), \
             patch("app.ai.circuit_breaker.get_breaker") as mock_breaker:
                mock_breaker.return_value.is_available.return_value = True
                result = provider.call_raw("sys", "usr", 500, 0.2, 30)
        assert isinstance(result, LLMResponse)
        assert result.total_tokens == 300
        assert result.prompt_tokens == 200
        assert result.completion_tokens == 100
        assert result.cost_usd >= 0
        assert result.provider == "groq"


class TestGeminiProvider:
    """
    Gemini has its own response shape (candidates[0].content.parts[0].text,
    usageMetadata) and imports get_breaker via `from ... import get_breaker`
    (a local binding), so patches target app.ai.providers.gemini.get_breaker
    -- not app.ai.circuit_breaker.get_breaker like the Groq tests.
    """

    def _make_response(self, text="ok response", prompt_tok=100, completion_tok=50):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": text}]}}],
            "usageMetadata": {
                "promptTokenCount": prompt_tok,
                "candidatesTokenCount": completion_tok,
                "totalTokenCount": prompt_tok + completion_tok,
            },
        }
        return mock_resp

    def test_successful_call_returns_text(self):
        from app.ai.providers.gemini import GeminiProvider
        provider = GeminiProvider()
        with patch("app.ai.providers.gemini.http_requests.post",
                   return_value=self._make_response("hello")), \
             patch("app.ai.providers.gemini.get_breaker") as mock_breaker:
                mock_breaker.return_value.is_available.return_value = True
                result = provider.call_raw("sys", "usr", 500, 0.2, 30)
        assert result.text == "hello"
        assert result.error is None

    def test_circuit_open_short_circuits(self):
        """When circuit is open, no HTTP call should be made."""
        from app.ai.providers.gemini import GeminiProvider
        provider = GeminiProvider()
        with patch("app.ai.providers.gemini.get_breaker") as mock_breaker:
            mock_breaker.return_value.is_available.return_value = False
            with patch("app.ai.providers.gemini.http_requests.post") as mock_post:
                result = provider.call_raw("sys", "usr", 500, 0.2, 30)
        mock_post.assert_not_called()
        assert "Circuit OPEN" in result.error

    def test_missing_api_key_returns_error(self):
        from app.ai.providers.gemini import GeminiProvider
        provider = GeminiProvider()
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}), \
             patch("app.ai.providers.gemini.get_breaker") as mock_breaker, \
             patch("app.ai.providers.gemini.GEMINI_API_KEY", ""):
            mock_breaker.return_value.is_available.return_value = True
            result = provider.call_raw("sys", "usr", 500, 0.2, 30)
        assert "GEMINI_API_KEY" in result.error

    def test_rate_limit_returns_error(self):
        from app.ai.providers.gemini import GeminiProvider
        provider = GeminiProvider()
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        with patch("app.ai.providers.gemini.http_requests.post", return_value=mock_resp), \
             patch("app.ai.providers.gemini.get_breaker") as mock_breaker:
                mock_breaker.return_value.is_available.return_value = True
                result = provider.call_raw("sys", "usr", 500, 0.2, 30)
        assert "RATE_LIMIT" in result.error
        assert result.text == ""
        mock_breaker.return_value.record_failure.assert_called()

    def test_bad_request_is_reported_without_opening_the_circuit(self):
        """
        This test used to assert `record_failure` WAS called on a 400, which
        encoded the defect rather than catching it.

        A 400 from Gemini is an invalid key or an unusable model — Google
        answers an invalid key with 400 and API_KEY_INVALID rather than 401.
        None of that recovers on its own, so opening the circuit replaces a
        precise error with "provider unavailable". That is how the Groq model
        outage stayed unreadable for four nights.
        """
        from app.ai.providers.base import is_configuration_error
        from app.ai.providers.gemini import GeminiProvider
        provider = GeminiProvider()
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "invalid request body"
        mock_resp.json.return_value = {"error": {"message": "API_KEY_INVALID"}}
        with patch("app.ai.providers.gemini.http_requests.post", return_value=mock_resp), \
             patch("app.ai.providers.gemini.get_breaker") as mock_breaker:
                mock_breaker.return_value.is_available.return_value = True
                result = provider.call_raw("sys", "usr", 500, 0.2, 30)
        assert is_configuration_error(result.error)
        assert "GEMINI_API_KEY" in result.error
        mock_breaker.return_value.record_failure.assert_not_called()

    def test_500_server_error_opens_circuit(self):
        from app.ai.providers.gemini import GeminiProvider
        provider = GeminiProvider()
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        with patch("app.ai.providers.gemini.http_requests.post", return_value=mock_resp), \
             patch("app.ai.providers.gemini.get_breaker") as mock_breaker:
                mock_breaker.return_value.is_available.return_value = True
                result = provider.call_raw("sys", "usr", 500, 0.2, 30)
        mock_breaker.return_value.record_failure.assert_called()
        assert "Server error" in result.error

    def test_timeout_records_circuit_failure(self):
        import requests as req_lib
        from app.ai.providers.gemini import GeminiProvider
        provider = GeminiProvider()
        with patch("app.ai.providers.gemini.http_requests.post",
                   side_effect=req_lib.exceptions.Timeout()), \
             patch("app.ai.providers.gemini.get_breaker") as mock_breaker:
                mock_breaker.return_value.is_available.return_value = True
                result = provider.call_raw("sys", "usr", 500, 0.2, 30)
        mock_breaker.return_value.record_failure.assert_called()
        assert result.error is not None

    def test_unexpected_response_format_returns_error(self):
        """Malformed candidates/parts shape must not raise -- returns an error LLMResponse."""
        from app.ai.providers.gemini import GeminiProvider
        provider = GeminiProvider()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"candidates": []}
        with patch("app.ai.providers.gemini.http_requests.post", return_value=mock_resp), \
             patch("app.ai.providers.gemini.get_breaker") as mock_breaker:
                mock_breaker.return_value.is_available.return_value = True
                result = provider.call_raw("sys", "usr", 500, 0.2, 30)
        assert "Unexpected response format" in result.error
        mock_breaker.return_value.record_failure.assert_called()

    def test_track_skips_zero_tokens(self):
        """
        _track with 0 tokens should be a no-op (no Redis writes). gemini.py
        imports get_redis *inside* _track (unlike groq.py's module-level
        import), so it always resolves the live redis_client singleton --
        no patch target needed, just reset + let the fake store share state.
        """
        rc.reset_client()
        with patch.dict(os.environ, {"REDIS_URL": ""}):
            r = rc.get_redis()
        from app.ai.providers.gemini import GeminiProvider
        provider = GeminiProvider()
        provider._track(0)
        import datetime
        today = datetime.date.today().isoformat()
        assert r.get(f"llm:tokens:gemini:{today}") is None

    def test_token_tracking_accumulates_correctly(self):
        rc.reset_client()
        with patch.dict(os.environ, {"REDIS_URL": ""}):
            r = rc.get_redis()
        from app.ai.providers.gemini import GeminiProvider
        provider = GeminiProvider()
        import datetime
        today = datetime.date.today().isoformat()
        tok_key = f"llm:tokens:gemini:{today}"
        provider._track(1500)
        provider._track(500)
        assert r.get(tok_key) == "2000"

    def test_llm_response_fields(self):
        from app.ai.providers.gemini import GeminiProvider
        from app.ai.providers.base import LLMResponse
        provider = GeminiProvider()
        resp = self._make_response("test output", 200, 100)
        with patch("app.ai.providers.gemini.http_requests.post", return_value=resp), \
             patch("app.ai.providers.gemini.get_breaker") as mock_breaker:
                mock_breaker.return_value.is_available.return_value = True
                result = provider.call_raw("sys", "usr", 500, 0.2, 30)
        assert isinstance(result, LLMResponse)
        assert result.total_tokens == 300
        assert result.prompt_tokens == 200
        assert result.completion_tokens == 100
        assert result.provider == "gemini"
        assert result.model == "gemini-1.5-flash"


class TestLLMResponse:

    def test_error_response_has_empty_text(self):
        from app.ai.providers.base import LLMResponse
        r = LLMResponse(text="", provider="groq", model="test", error="fail")
        assert r.text == ""
        assert r.error == "fail"
        assert r.is_error is True

    def test_success_response_no_error(self):
        from app.ai.providers.base import LLMResponse
        r = LLMResponse(text="hello", provider="groq", model="test")
        assert r.is_error is False
        assert r.error is None
