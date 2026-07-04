"""
tests/test_observability_brain.py

Two things:
  1. startup_check() warns loudly (but does not fail) when METRICS_AUTH_TOKEN /
     MCP_API_KEY are unset — so a public /health or a dead plugin is caught at boot.
  2. The brain is explainable: decisions carry their rationale ("why"), and the
     recalled context surfaces it.
"""

import logging

import pytest

from app.core import webhook_security
from app.intelligence import memory


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    from app.core import redis_client

    redis_client.reset_client()
    for v in ("LLM_LOCAL_ONLY", "LLM_PREFER_LOCAL", "MEMORY_ALLOW_CLOUD"):
        monkeypatch.delenv(v, raising=False)
    yield
    redis_client.reset_client()


class TestStartupWarnings:
    def test_warns_when_metrics_token_unset(self, monkeypatch, caplog):
        monkeypatch.delenv("METRICS_AUTH_TOKEN", raising=False)
        with caplog.at_level(logging.WARNING):
            webhook_security.startup_check()  # must NOT raise
        assert any("metrics_unauthed" in r.message for r in caplog.records)

    def test_warns_when_mcp_key_unset(self, monkeypatch, caplog):
        monkeypatch.delenv("MCP_API_KEY", raising=False)
        with caplog.at_level(logging.WARNING):
            webhook_security.startup_check()
        assert any("mcp_unconfigured" in r.message for r in caplog.records)

    def test_no_warnings_when_all_set(self, monkeypatch, caplog):
        monkeypatch.setenv("METRICS_AUTH_TOKEN", "a-strong-token")
        monkeypatch.setenv("MCP_API_KEY", "a-strong-key")
        with caplog.at_level(logging.WARNING):
            webhook_security.startup_check()
        assert not any(
            "metrics_unauthed" in r.message or "mcp_unconfigured" in r.message
            for r in caplog.records
        )

    def test_still_raises_on_missing_credentials(self, monkeypatch):
        monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
        with pytest.raises(RuntimeError):
            webhook_security.startup_check()


class TestExplainableBrain:
    def test_decision_stores_rationale(self, monkeypatch):
        monkeypatch.setenv("LLM_LOCAL_ONLY", "1")
        assert memory.remember_decision(
            "o/r",
            "use Redis lists for the event queue",
            why="Celery is too heavy for the 512MB free tier",
        )
        items = memory.recall("o/r", "redis lists event queue")
        assert items
        assert items[0].kind == "decision"
        assert items[0].meta.get("why", "").startswith("Celery is too heavy")

    def test_recall_context_surfaces_why(self, monkeypatch):
        monkeypatch.setenv("LLM_LOCAL_ONLY", "1")
        memory.remember_decision(
            "o/r",
            "prefer local Ollama for private repos",
            why="source code must never reach a third-party API",
        )
        ctx = memory.recall_context("o/r", "local ollama private repos")
        assert "why:" in ctx
        assert "third-party API" in ctx

    def test_decision_without_why_still_works(self, monkeypatch):
        monkeypatch.setenv("LLM_LOCAL_ONLY", "1")
        assert memory.remember_decision("o/r", "adopt conventional commits")
        ctx = memory.recall_context("o/r", "conventional commits")
        assert "conventional commits" in ctx
        assert "why:" not in ctx  # no rationale → no dangling "why"
