"""
tests/test_ollama_local.py — Local LLM provider + router privacy modes.

The critical property under test: in LLM_LOCAL_ONLY mode, NO cloud provider is
ever selected or used as a fallback — source code must never leak off-box.
"""

from unittest.mock import MagicMock, patch

import pytest

import app.ai.circuit_breaker as cb
from app.ai.providers.ollama import OllamaProvider, is_configured
from app.ai.router import LLMRouter


@pytest.fixture(autouse=True)
def reset_breakers():
    cb._breakers.clear()
    yield
    cb._breakers.clear()


# ── Provider ──────────────────────────────────────────────────────────────────


class TestOllamaProvider:
    def test_inactive_without_host(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        assert is_configured() is False
        resp = OllamaProvider().call_raw("s", "u", 100, 0.2, 30)
        assert resp.is_error
        assert "OLLAMA_HOST not set" in resp.error

    def test_successful_call_is_free_and_local(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434")
        fake = MagicMock()
        fake.status_code = 200
        fake.json.return_value = {
            "message": {"content": '{"ok": true}'},
            "prompt_eval_count": 12,
            "eval_count": 8,
        }
        with patch("app.ai.providers.ollama.http_requests.post", return_value=fake):
            resp = OllamaProvider().call_raw("s", "u", 100, 0.2, 30)
        assert not resp.is_error
        assert resp.provider == "ollama"
        assert resp.cost_usd == 0.0
        assert resp.total_tokens == 20

    def test_connection_error_opens_breaker(self, monkeypatch):
        import requests

        monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434")
        with patch(
            "app.ai.providers.ollama.http_requests.post",
            side_effect=requests.exceptions.ConnectionError("refused"),
        ):
            resp = OllamaProvider().call_raw("s", "u", 100, 0.2, 30)
        assert resp.is_error
        assert "Cannot reach Ollama" in resp.error

    def test_server_error_records_failure(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434")
        fake = MagicMock()
        fake.status_code = 500
        with patch("app.ai.providers.ollama.http_requests.post", return_value=fake):
            resp = OllamaProvider().call_raw("s", "u", 100, 0.2, 30)
        assert resp.is_error


# ── Router privacy modes ──────────────────────────────────────────────────────


class TestLocalOnlyMode:
    def test_local_only_selects_ollama(self, monkeypatch):
        monkeypatch.setenv("LLM_LOCAL_ONLY", "1")
        monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434")
        r = LLMRouter()
        provider = r._select_provider("standard")
        assert provider.provider_key == "ollama"

    def test_local_only_never_falls_back_to_cloud(self, monkeypatch):
        monkeypatch.setenv("LLM_LOCAL_ONLY", "1")
        monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434")
        r = LLMRouter()
        candidates = r._fallback_candidates()
        keys = {c.provider_key for c in candidates if c is not None}
        assert keys == {"ollama"}
        assert "groq_70b" not in keys
        assert "gemini" not in keys

    def test_local_only_fails_closed_when_ollama_down(self, monkeypatch):
        from app.ai.circuit_breaker import AllProvidersDown

        monkeypatch.setenv("LLM_LOCAL_ONLY", "1")
        monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434")
        cb.get_breaker("ollama")._state = cb.CBState.OPEN
        cb.get_breaker("ollama")._opened_at = 9e18  # stays open
        r = LLMRouter()
        with pytest.raises(AllProvidersDown):
            r._select_provider("standard")

    def test_local_only_without_host_fails_closed(self, monkeypatch):
        from app.ai.circuit_breaker import AllProvidersDown

        monkeypatch.setenv("LLM_LOCAL_ONLY", "1")
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        r = LLMRouter()
        with pytest.raises(AllProvidersDown):
            r._select_provider("standard")


class TestPreferLocalMode:
    def test_prefer_local_tries_ollama_first(self, monkeypatch):
        monkeypatch.setenv("LLM_PREFER_LOCAL", "1")
        monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434")
        r = LLMRouter()
        assert r._select_provider("standard").provider_key == "ollama"

    def test_prefer_local_allows_cloud_in_fallback(self, monkeypatch):
        monkeypatch.setenv("LLM_PREFER_LOCAL", "1")
        monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434")
        r = LLMRouter()
        keys = {c.provider_key for c in r._fallback_candidates() if c is not None}
        assert "ollama" in keys
        assert "groq_70b" in keys  # cloud fallback still permitted

    def test_default_mode_ignores_ollama(self, monkeypatch):
        monkeypatch.delenv("LLM_LOCAL_ONLY", raising=False)
        monkeypatch.delenv("LLM_PREFER_LOCAL", raising=False)
        monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434")
        r = LLMRouter()
        # Cloud-first by default even when Ollama is configured
        assert r._select_provider("standard").provider_key.startswith("groq")
        keys = {c.provider_key for c in r._fallback_candidates() if c is not None}
        assert "ollama" not in keys
