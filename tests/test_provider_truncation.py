"""
tests/test_provider_truncation.py

No provider read the stop reason. A completion cut at max_tokens came back as
a success: /changelog, /explain and the PR summary published half an answer
as the whole one, and JSON callers saw a fragment and called it garbage. A
null `content` (a reasoning model that spent its budget thinking) raised
AttributeError instead of returning an error.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.ai.providers.base import TRUNCATED, AnswerTruncated, LLMResponse, was_cut_off


def _openai_shaped(content, finish):
    r = MagicMock(status_code=200)
    r.json.return_value = {
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 800, "total_tokens": 810},
    }
    return r


@pytest.mark.parametrize("reason,cut", [
    ("length", True), ("MAX_TOKENS", True), ("max_tokens", True),
    ("stop", False), ("STOP", False), (None, False), ("", False),
])
def test_every_spelling_of_the_limit(reason, cut):
    assert was_cut_off(reason) is cut


class TestGroq:
    def _provider(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "k")
        from app.ai.providers.groq import GroqProvider

        return GroqProvider(model="openai/gpt-oss-120b", provider_key="groq_70b")

    def test_a_cut_answer_is_an_error_not_text(self, monkeypatch):
        p = self._provider(monkeypatch)
        with patch("app.ai.providers.groq.http_requests.post",
                   return_value=_openai_shaped("## [1.4.0]\n### Added\n- x\n### Fix", "length")):
            text, meta = p.ask_text("s", "u", 800, 30)
        assert text == ""
        assert meta.error.startswith(TRUNCATED)

    def test_a_null_content_is_not_a_crash(self, monkeypatch):
        p = self._provider(monkeypatch)
        with patch("app.ai.providers.groq.http_requests.post",
                   return_value=_openai_shaped(None, "length")):
            text, meta = p.ask_text("s", "u", 800, 30)
        assert text == "" and meta.error.startswith(TRUNCATED)

    def test_a_complete_answer_is_unchanged(self, monkeypatch):
        p = self._provider(monkeypatch)
        with patch("app.ai.providers.groq.http_requests.post",
                   return_value=_openai_shaped("  done  ", "stop")):
            text, meta = p.ask_text("s", "u", 800, 30)
        assert text == "done" and meta.error is None

    def test_the_circuit_is_not_charged(self, monkeypatch):
        """The provider answered; it is healthy."""
        from app.ai.circuit_breaker import get_breaker

        p = self._provider(monkeypatch)
        breaker = get_breaker("groq_70b")
        breaker.record_failure("earlier")
        with patch("app.ai.providers.groq.http_requests.post",
                   return_value=_openai_shaped("x", "length")):
            p.ask_text("s", "u", 800, 30)
        assert breaker._failures == 0, "a cut-off answer was charged as a provider failure"


class TestGemini:
    def test_a_cut_candidate_with_no_parts_is_truncation_not_a_bad_format(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        from app.ai.providers.gemini import GeminiProvider

        r = MagicMock(status_code=200)
        r.json.return_value = {"candidates": [{"content": {}, "finishReason": "MAX_TOKENS"}],
                               "usageMetadata": {}}
        with patch("app.ai.providers.gemini.http_requests.post", return_value=r):
            text, meta = GeminiProvider().ask_text("s", "u", 800, 30)
        assert text == "" and meta.error.startswith(TRUNCATED)


class TestOllama:
    def test_done_reason_length_is_truncation(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434")
        from app.ai.providers.ollama import OllamaProvider

        r = MagicMock(status_code=200)
        r.json.return_value = {"message": {"content": "half"}, "done_reason": "length"}
        with patch("app.ai.providers.ollama.http_requests.post", return_value=r):
            resp = OllamaProvider(min_timeout=0).call_raw("s", "u", 64, 0.2, 5)
        assert resp.error.startswith(TRUNCATED)


class TestOpenRouter:
    def _provider(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        from app.ai.providers.openrouter import OpenRouterProvider

        return OpenRouterProvider()

    def test_ask_and_ask_text(self, monkeypatch):
        p = self._provider(monkeypatch)
        with patch.object(p, "_post", return_value=_openai_shaped('{"a": 1', "length")):
            result, meta = p.ask("s", "u", 800, 0.2, 30)
        assert meta.error.startswith(TRUNCATED) and "a" not in result
        with patch.object(p, "_post", return_value=_openai_shaped(None, "length")):
            text, meta = p.ask_text("s", "u", 800, 30)
        assert text == "" and meta.error.startswith(TRUNCATED)


class TestTheRouterSaysWhatHappened:
    def test_all_cut_off_is_answer_truncated_not_an_outage(self):
        from app.ai.router import _raise_unanswered

        with pytest.raises(AnswerTruncated):
            _raise_unanswered(LLMResponse(text="", provider="groq", model="m",
                                          error=f"{TRUNCATED}: limit"))

    def test_anything_else_is_still_providers_down(self):
        from app.ai.circuit_breaker import AllProvidersDown
        from app.ai.router import _raise_unanswered

        with pytest.raises(AllProvidersDown):
            _raise_unanswered(LLMResponse(text="", provider="groq", model="m", error="Server error 503"))

    def test_answer_truncated_is_not_retried(self):
        from app.core.transient import is_transient

        assert not is_transient(AnswerTruncated("x"))


def test_the_pr_report_names_the_cut_off():
    from app.handlers import pull_request as pr_mod

    cfg = MagicMock()
    cfg.pr_enabled.return_value = True
    cfg.get.side_effect = lambda *a, **k: k.get("default", True)
    cfg.footer = ""
    payload = {
        "action": "synchronize", "repository": {"full_name": "o/r"}, "installation": {"id": 1},
        "pull_request": {"number": 3, "title": "t", "body": "", "user": {"login": "a"},
                         "head": {"sha": "s"}, "base": {"ref": "main"}},
    }
    with patch.object(pr_mod, "get_installation_token", return_value="t"), \
         patch.object(pr_mod, "load_config", return_value=cfg), \
         patch.object(pr_mod, "gh_get", return_value={}), \
         patch("app.github.helpers.pr_files", return_value=[{"filename": "a.py", "patch": "+x"}]), \
         patch.object(pr_mod, "_analyze_pr", return_value="a"), \
         patch.object(pr_mod, "_build_pr_summary", side_effect=AnswerTruncated("TRUNCATED: x")), \
         patch.object(pr_mod, "_review_code", return_value=("", [])), \
         patch.object(pr_mod, "_detect_test_gaps", return_value=""), \
         patch.object(pr_mod, "upsert_sticky") as upsert, \
         patch.object(pr_mod, "update_sticky_if_present") as update:
        pr_mod.handle(payload)
    body = (upsert.call_args or update.call_args)[0][4]
    assert "summary — the model's answer was cut off at its length limit" in body

