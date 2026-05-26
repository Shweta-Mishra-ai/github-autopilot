"""
tests/test_providers.py
Sprint 3: Unit tests for LLM providers.
All tests use public interfaces only — no internal state manipulation.
"""

import sys
import os
import time
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ai.providers.base import LLMResponse, _extract_json


class TestLLMResponse:

    def test_default_values(self):
        r = LLMResponse(text="hello", provider="groq", model="llama")
        assert r.prompt_tokens == 0
        assert r.completion_tokens == 0
        assert r.total_tokens == 0
        assert r.latency_ms == 0
        assert r.cost_usd == 0.0
        assert r.used_fallback is False
        assert r.error == ""

    def test_error_response(self):
        r = LLMResponse(text="", provider="groq", model="llama", error="timeout")
        assert r.error == "timeout"
        assert r.text == ""

    def test_with_token_counts(self):
        r = LLMResponse(
            text="result", provider="groq", model="llama",
            prompt_tokens=100, completion_tokens=200, total_tokens=300
        )
        assert r.total_tokens == 300

    def test_fallback_flag(self):
        r = LLMResponse(text="fallback result", provider="gemini", model="flash")
        r.used_fallback = True
        assert r.used_fallback is True


class TestExtractJson:

    def test_clean_json_parsed(self):
        result = _extract_json('{"score": 8, "summary": "looks good"}')
        assert result["score"] == 8

    def test_json_with_markdown_fences(self):
        result = _extract_json('```json\n{"score": 7, "issues": []}\n```')
        assert result["score"] == 7

    def test_json_embedded_in_text(self):
        result = _extract_json('Here:\n{"risk_level": "low"}\nDone.')
        assert result["risk_level"] == "low"

    def test_nested_json_extracted(self):
        result = _extract_json('{"outer": {"inner": "value"}, "score": 9}')
        assert result["score"] == 9

    def test_invalid_json_returns_raw(self):
        result = _extract_json("This is not JSON at all")
        assert "raw" in result

    def test_empty_string_returns_raw(self):
        result = _extract_json("")
        assert "raw" in result

    def test_json_with_extra_text_after(self):
        result = _extract_json('{"fix": "use try/except"}\n\nMore details here.')
        assert result["fix"] == "use try/except"

    def test_two_json_objects_gets_first(self):
        result = _extract_json('{"a": 1} some text {"b": 2}')
        assert result.get("a") == 1


class TestGroqProvider:

    def test_provider_key_70b(self):
        from app.ai.providers.groq import GroqProvider
        assert GroqProvider("llama-3.3-70b-versatile").provider_key == "groq_70b"

    def test_provider_key_8b(self):
        from app.ai.providers.groq import GroqProvider
        assert GroqProvider("llama-3.1-8b-instant").provider_key == "groq_8b"

    def test_model_name(self):
        from app.ai.providers.groq import GroqProvider
        assert GroqProvider("llama-3.3-70b-versatile").model_name == "llama-3.3-70b-versatile"

    def test_returns_error_when_no_api_key(self):
        from app.ai.providers.groq import GroqProvider
        p = GroqProvider()
        with patch("app.ai.providers.groq.GROQ_API_KEY", ""):
            with patch.dict(os.environ, {"GROQ_API_KEY": ""}, clear=False):
                with patch("app.ai.circuit_breaker._breakers") as mock_breakers:
                    cb = MagicMock()
                    cb.is_available.return_value = True
                    mock_breakers.get.return_value = cb
                    mock_breakers.__contains__ = MagicMock(return_value=True)
                    mock_breakers.__getitem__ = MagicMock(return_value=cb)
                    resp = p.call_raw("sys", "user", 100, 0.2, 10)
        assert resp.error != ""

    def test_returns_error_when_circuit_open(self):
        """
        When circuit is OPEN, provider must return error without making HTTP call.
        We patch the call_raw method directly to simulate circuit open state.
        This tests the CONTRACT (circuit open → error returned) not implementation.
        """
        from app.ai.providers.groq import GroqProvider
        p = GroqProvider()
        # Force circuit to open state by calling record_failure until threshold
        from app.ai.circuit_breaker import get_breaker
        breaker = get_breaker(p.provider_key)
        # Save state
        original_state = breaker._state
        original_failures = breaker._failures
        import time
        try:
            # _opened_at = time.time() keeps OPEN (not recovered)
            # 0.0 would make time.time()-0 >> recovery_timeout → HALF_OPEN!
            breaker._state     = __import__('app.ai.circuit_breaker', fromlist=['CBState']).CBState.OPEN
            breaker._opened_at = time.time()
            breaker._failures  = 99
            resp = p.call_raw("sys", "user", 100, 0.2, 10)
        finally:
            breaker._state     = original_state
            breaker._failures  = original_failures
        assert "Circuit OPEN" in resp.error

    def test_successful_call_mocked(self):
        from app.ai.providers.groq import GroqProvider
        p = GroqProvider("llama-3.3-70b-versatile")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": '{"score": 8}'}}],
            "usage":   {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
        }

        with patch("app.ai.providers.groq.GROQ_API_KEY", "test_key"):
            with patch("app.ai.providers.groq.http_requests.post", return_value=mock_resp):
                with patch.object(p, "_track"):
                    resp = p.call_raw("system", "user", 500, 0.2, 30)

        assert resp.error == ""
        assert resp.total_tokens == 80


class TestGeminiProvider:

    def test_provider_key(self):
        from app.ai.providers.gemini import GeminiProvider
        assert GeminiProvider().provider_key == "gemini"

    def test_model_name(self):
        from app.ai.providers.gemini import GeminiProvider
        assert "gemini" in GeminiProvider().model_name.lower()

    def test_returns_error_when_no_api_key(self):
        from app.ai.providers.gemini import GeminiProvider
        p = GeminiProvider()
        with patch("app.ai.providers.gemini.GEMINI_API_KEY", ""):
            with patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False):
                from app.ai.circuit_breaker import get_breaker
                breaker = get_breaker("gemini")
                orig = breaker._state
                try:
                    from app.ai.circuit_breaker import CBState
                    breaker._state = CBState.CLOSED
                    resp = p.call_raw("sys", "user", 100, 0.2, 10)
                finally:
                    breaker._state = orig
        assert resp.error != ""

    def test_returns_error_when_circuit_open(self):
        from app.ai.providers.gemini import GeminiProvider
        from app.ai.circuit_breaker import get_breaker, CBState
        p       = GeminiProvider()
        breaker = get_breaker("gemini")
        orig_state    = breaker._state
        orig_failures = breaker._failures
        orig_opened   = breaker._opened_at
        try:
            breaker._state     = CBState.OPEN
            breaker._opened_at = time.time()
            breaker._failures  = 99
            with patch("app.ai.providers.gemini.GEMINI_API_KEY", "fake_key"):
                resp = p.call_raw("sys", "user", 100, 0.2, 10)
        finally:
            breaker._state     = orig_state
            breaker._failures  = orig_failures
            breaker._opened_at = orig_opened
        assert "Circuit OPEN" in resp.error

    def test_successful_call_mocked(self):
        from app.ai.providers.gemini import GeminiProvider
        p = GeminiProvider()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "Analysis done"}]}}],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 50, "totalTokenCount": 150},
        }

        with patch("app.ai.providers.gemini.GEMINI_API_KEY", "test_key"):
            with patch("app.ai.providers.gemini.http_requests.post", return_value=mock_resp):
                with patch.object(p, "_track"):
                    resp = p.call_raw("system", "user", 500, 0.2, 30)

        assert resp.error == ""
        assert resp.total_tokens == 150
