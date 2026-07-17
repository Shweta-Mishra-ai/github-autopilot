"""
tests/test_integration_privacy_and_brain.py — Integration tests for the two
V6 "brain" guarantees, exercised through their full call paths rather than a
single internal method.

1. LLM_LOCAL_ONLY must NEVER call a cloud provider's call_raw(), even when the
   local model is unreachable and the router goes through its fallback logic.
   Existing unit tests check `_select_provider`/`_fallback_candidates` directly;
   this exercises the full `LLMRouter.ask()` entry point real callers use.

2. Memory -> encrypted backup -> restore is a full round trip: explainable
   decisions survive it, and the ciphertext genuinely contains no plaintext.
"""

from __future__ import annotations


import pytest

from app.ai.circuit_breaker import AllProvidersDown
from app.ai.router import LLMRouter
from app.core import memory_backup
from app.intelligence import memory


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    from app.core import redis_client
    import app.ai.circuit_breaker as cb

    redis_client.reset_client()
    cb._breakers.clear()
    for v in ("LLM_LOCAL_ONLY", "LLM_PREFER_LOCAL", "MEMORY_ALLOW_CLOUD", "MEMORY_BACKUP_KEY"):
        monkeypatch.delenv(v, raising=False)
    yield
    redis_client.reset_client()
    cb._breakers.clear()


class TestLocalOnlyNeverTouchesCloud:
    """
    Ollama's own network failure is simulated by mocking OllamaProvider.call_raw
    directly (rather than pointing at a real unreachable host) — this is a
    deliberate choice: connection-refused timing is OS/CI-runner dependent and
    was adding several real seconds per test for no assertion value. What
    actually matters — and what these tests spy on — is whether the router
    ever calls a *cloud* provider's call_raw in local-only mode; that stays a
    genuine end-to-end check through the real LLMRouter.ask()/ask_text() path.
    """

    def test_ask_fails_closed_without_calling_any_cloud_provider(self, monkeypatch):
        monkeypatch.setenv("LLM_LOCAL_ONLY", "1")
        monkeypatch.setenv("OLLAMA_HOST", "http://ollama.invalid")
        monkeypatch.setenv("GROQ_API_KEY", "would-be-real-key")
        monkeypatch.setenv("GEMINI_API_KEY", "would-be-real-key")
        monkeypatch.setenv("OPENROUTER_API_KEY", "would-be-real-key")

        from app.ai.providers.base import LLMResponse

        def _ollama_down(self, system, user, max_tokens, temperature, timeout):
            return LLMResponse(text="", provider="ollama", model=self.model_name, error="down")

        def _leak(*a, **k):
            raise AssertionError("LEAK: a cloud provider was called in LLM_LOCAL_ONLY mode")

        monkeypatch.setattr("app.ai.providers.ollama.OllamaProvider.call_raw", _ollama_down)
        monkeypatch.setattr("app.ai.providers.groq.GroqProvider.call_raw", _leak)
        monkeypatch.setattr("app.ai.providers.gemini.GeminiProvider.call_raw", _leak, raising=False)
        monkeypatch.setattr(
            "app.ai.providers.openrouter.OpenRouterProvider.call_raw", _leak, raising=False
        )

        router = LLMRouter()
        with pytest.raises(AllProvidersDown):
            router.ask("system", "proprietary source code must not leak", task="fix_command")

    def test_ask_text_also_fails_closed(self, monkeypatch):
        monkeypatch.setenv("LLM_LOCAL_ONLY", "1")
        monkeypatch.setenv("OLLAMA_HOST", "http://ollama.invalid")
        monkeypatch.setenv("GROQ_API_KEY", "would-be-real-key")

        from app.ai.providers.base import LLMResponse

        def _ollama_down(self, system, user, max_tokens, temperature, timeout):
            return LLMResponse(text="", provider="ollama", model=self.model_name, error="down")

        def _leak(*a, **k):
            raise AssertionError("LEAK: cloud provider called via ask_text in local-only mode")

        monkeypatch.setattr("app.ai.providers.ollama.OllamaProvider.call_raw", _ollama_down)
        monkeypatch.setattr("app.ai.providers.groq.GroqProvider.call_raw", _leak)

        router = LLMRouter()
        with pytest.raises(AllProvidersDown):
            router.ask_text("system", "sensitive text", task="explain")

    def test_prefer_local_falls_back_to_cloud_when_local_fails(self, monkeypatch):
        """The softer mode (PREFER_LOCAL) is allowed to use cloud as a fallback."""
        monkeypatch.setenv("LLM_PREFER_LOCAL", "1")
        monkeypatch.setenv("OLLAMA_HOST", "http://ollama.invalid")
        monkeypatch.setenv("GROQ_API_KEY", "fake")

        from app.ai.providers.base import LLMResponse

        def _ollama_down(self, system, user, max_tokens, temperature, timeout):
            return LLMResponse(text="", provider="ollama", model=self.model_name, error="down")

        def _ok(self, system, user, max_tokens, temperature, timeout):
            return LLMResponse(text='{"ok": true}', provider="groq", model=self.model_name)

        monkeypatch.setattr("app.ai.providers.ollama.OllamaProvider.call_raw", _ollama_down)
        monkeypatch.setattr("app.ai.providers.groq.GroqProvider.call_raw", _ok)

        router = LLMRouter()
        result, meta = router.ask("system", "user", task="fix_command")
        assert meta.provider == "groq"  # fell back to cloud after local failed
        assert result == {"ok": True}


class TestMemoryBackupRoundTripIntegration:
    def test_full_round_trip_through_router_privacy_guard(self, monkeypatch):
        """
        remember_decision -> recall_context (privacy-gated) -> encrypt -> wipe
        -> decrypt/restore -> recall again. Exercises memory.py and
        memory_backup.py together, the way an operator actually uses them.
        """
        monkeypatch.setenv("LLM_LOCAL_ONLY", "1")
        monkeypatch.setenv("MEMORY_BACKUP_KEY", memory_backup.generate_key())
        repo = "integration-test/repo"

        memory.remember_decision(
            repo, "use Redis lists for the event queue", why="Celery too heavy for the free tier"
        )
        memory.remember(repo, "auth uses JWT RS256 with 15-minute expiry", kind="pattern")

        ctx = memory.recall_context(repo, "redis queue decision rationale")
        assert "why:" in ctx
        assert "Celery" in ctx

        blob = memory_backup.export_encrypted([repo])
        assert blob is not None
        # Check reasonably long, near-unique substrings — a short one like "JWT"
        # (3 chars) has a real, measurable chance of appearing by pure coincidence
        # in ~700 chars of high-entropy base64 ciphertext (~0.2% per run observed
        # empirically), which is exactly the kind of statistically flaky assertion
        # that must not ship. Longer substrings make the collision probability
        # astronomically small while still proving no plaintext leaked.
        assert b"Celery too heavy" not in blob
        assert b"JWT RS256" not in blob

        memory.clear(repo)
        assert memory.count(repo) == 0

        restored = memory_backup.import_encrypted(blob)
        assert restored == 1
        assert memory.count(repo) == 2

        ctx_after = memory.recall_context(repo, "redis queue decision rationale")
        assert "why:" in ctx_after
        assert "Celery" in ctx_after

    def test_memory_never_reaches_cloud_prompt_by_default(self, monkeypatch):
        """
        Default (no privacy env vars set) is cloud mode: memory must exist and
        be recallable directly, but recall_context() — the function actually
        wired into the outgoing prompt — must return nothing.
        """
        repo = "integration-test/cloud-default"
        memory.remember(repo, "internal architecture decision — sensitive", kind="decision")
        assert memory.count(repo) == 1
        assert memory.recall(repo, "architecture") != []  # still stored/searchable
        assert memory.recall_context(repo, "architecture") == ""  # but never injected
