"""
tests/test_providers.py
Sprint 3: Unit tests for LLM providers.

Tests all providers without making real API calls.
Uses mocking throughout — no network, no cost.

Run: python -m pytest tests/test_providers.py -v
"""

import sys
import os
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
        text   = '{"score": 8, "summary": "looks good"}'
        result = _extract_json(text)
        assert result["score"] == 8
        assert result["summary"] == "looks good"

    def test_json_with_markdown_fences(self):
        text   = '```json\n{"score": 7, "issues": []}\n```'
        result = _extract_json(text)
        assert result["score"] == 7

    def test_json_embedded_in_text(self):
        text   = 'Here is my analysis:\n{"risk_level": "low"}\nLet me know.'
        result = _extract_json(text)
        assert result["risk_level"] == "low"

    def test_nested_json_extracted(self):
        text   = '{"outer": {"inner": "value"}, "score": 9}'
        result = _extract_json(text)
        assert result["score"] == 9
        assert result["outer"]["inner"] == "value"

    def test_invalid_json_returns_raw(self):
        text   = "This is not JSON at all"
        result = _extract_json(text)
        assert "raw" in result
        assert result["raw"] == text

    def test_empty_string_returns_raw(self):
        result = _extract_json("")
        assert "raw" in result

    def test_json_with_extra_text_after(self):
        text   = '{"fix": "use try/except"}\n\nLet me know if you need more details.'
        result = _extract_json(text)
        assert result["fix"] == "use try/except"

    def test_two_json_objects_gets_first(self):
        """Brace-depth scan should get the first complete object."""
        text   = '{"a": 1} some text {"b": 2}'
        result = _extract_json(text)
        assert result.get("a") == 1


class TestGroqProvider:

    def test_provider_key_70b(self):
        from app.ai.providers.groq import GroqProvider
        p = GroqProvider("llama-3.3-70b-versatile")
        assert p.provider_key == "groq_70b"

    def test_provider_key_8b(self):
        from app.ai.providers.groq import GroqProvider
        p = GroqProvider("llama-3.1-8b-instant")
        assert p.provider_key == "groq_8b"

    def test_model_name(self):
        from app.ai.providers.groq import GroqProvider
        p = GroqProvider("llama-3.3-70b-versatile")
        assert p.model_name == "llama-3.3-70b-versatile"

    def test_returns_error_when_no_api_key(self):
        from app.ai.providers.groq import GroqProvider
        p = GroqProvider()
        with patch("app.ai.providers.groq.GROQ_API_KEY", ""):
            with patch("app.ai.providers.groq.get_breaker") as mock_cb:
                cb = MagicMock()
                cb.is_available.return_value = True
                mock_cb.return_value = cb
                resp = p.call_raw("sys", "user", 100, 0.2, 10)
                assert resp.error != ""

    def test_returns_error_when_circuit_open(self):
        from app.ai.providers.groq import GroqProvider
        p = GroqProvider()
        with patch("app.ai.providers.groq.get_breaker") as mock_cb:
            cb = MagicMock()
            cb.is_available.return_value = False
            mock_cb.return_value = cb
            resp = p.call_raw("sys", "user", 100, 0.2, 10)
            assert "Circuit OPEN" in resp.error

    def test_successful_call_mocked(self):
        from app.ai.providers.groq import GroqProvider
        import requests as req

        p = GroqProvider("llama-3.3-70b-versatile")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": '{"score": 8}'}}],
            "usage":   {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
        }

        with patch("app.ai.circuit_breaker.get_breaker") as mock_cb:
            cb = MagicMock()
            cb.is_available.return_value = True
            cb.record_success = MagicMock()
            mock_cb.return_value = cb

            with patch("app.ai.providers.groq.GROQ_API_KEY", "test_key"):
                with patch("app.ai.providers.groq.http_requests.post", return_value=mock_resp):
                    with patch.object(p, "_track"):
                        resp = p.call_raw("system", "user", 500, 0.2, 30)

        assert resp.error == ""
        assert resp.text == '{"score": 8}'
        assert resp.total_tokens == 80


class TestGeminiProvider:

    def test_provider_key(self):
        from app.ai.providers.gemini import GeminiProvider
        p = GeminiProvider()
        assert p.provider_key == "gemini"

    def test_model_name(self):
        from app.ai.providers.gemini import GeminiProvider
        p = GeminiProvider()
        assert "gemini" in p.model_name.lower()

    def test_returns_error_when_no_api_key(self):
        from app.ai.providers.gemini import GeminiProvider
        p = GeminiProvider()
        with patch("app.ai.providers.gemini.GEMINI_API_KEY", ""):
            with patch("app.ai.providers.gemini.get_breaker") as mock_cb:
                cb = MagicMock()
                cb.is_available.return_value = True
                mock_cb.return_value = cb
                resp = p.call_raw("sys", "user", 100, 0.2, 10)
                assert resp.error != ""

    def test_returns_error_when_circuit_open(self):
        from app.ai.providers.gemini import GeminiProvider
        from app.ai import circuit_breaker as cb_module
        p = GeminiProvider()
        original = cb_module._breakers["gemini"]
        try:
            mock_cb = MagicMock()
            mock_cb.is_available.return_value = False
            cb_module._breakers["gemini"] = mock_cb
            resp = p.call_raw("sys", "user", 100, 0.2, 10)
        finally:
            cb_module._breakers["gemini"] = original
        assert "Circuit OPEN" in resp.error

    def test_successful_call_mocked(self):
        from app.ai.providers.gemini import GeminiProvider

        p = GeminiProvider()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "Analysis complete"}]}}],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 50, "totalTokenCount": 150},
        }

        with patch("app.ai.circuit_breaker.get_breaker") as mock_cb:
            cb = MagicMock()
            cb.is_available.return_value = True
            cb.record_success = MagicMock()
            mock_cb.return_value = cb
            with patch("app.ai.providers.gemini.GEMINI_API_KEY", "test_key"):
                with patch("app.ai.providers.gemini.http_requests.post", return_value=mock_resp):
                    with patch.object(p, "_track"):
                        resp = p.call_raw("system", "user", 500, 0.2, 30)

        assert resp.error == ""
        assert resp.text == "Analysis complete"
        assert resp.total_tokens == 150
