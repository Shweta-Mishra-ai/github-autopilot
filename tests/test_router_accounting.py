"""
AI-layer accounting defects found by the 2026-10-08 audit, each pinned here.
"""

import datetime
from unittest.mock import MagicMock, patch

from app.ai.providers.base import LLMResponse
from app.ai.router import LLMRouter, _utc_today


class _KV(dict):
    def get(self, k):
        return super().get(k)

    def set(self, k, v, **kw):
        self[k] = str(v)
        return True

    def incr(self, k):
        self[k] = str(int(super().get(k) or 0) + 1)
        return int(self[k])

    def incrby(self, k, n):
        self[k] = str(int(super().get(k) or 0) + int(n))
        return int(self[k])

    def expire(self, *a):
        return True


def _provider(key, error=None, tokens=100):
    p = MagicMock()
    p.provider_key = key
    meta = LLMResponse(text='{"ok": 1}', provider="groq" if key.startswith("groq") else key,
                       model="m", total_tokens=tokens, error=error)
    p.ask.return_value = ({"ok": 1} if not error else {"error": error}, meta)
    return p


class TestFallbackSkipsTheSlotThatFailed:
    def test_a_failed_groq_slot_is_not_called_again(self):
        r = LLMRouter()
        failed = _provider("groq_70b", error="TIMEOUT")
        backup = _provider("groq_8b")
        with patch.object(r, "_select_provider", return_value=failed), \
             patch.object(r, "_fallback_candidates", return_value=[failed, backup]), \
             patch.object(r, "_call_provider", side_effect=lambda p, *a: p.ask(*a)), \
             patch.object(r, "_log_and_track") as track, \
             patch("app.ai.router._charge"):
            r.ask("s", "u", task="explain")
        assert failed.ask.call_count == 1, "the failed slot was retried by the fallback"
        assert backup.ask.call_count == 1
        assert track.call_args[0][2] == "groq_8b", "usage is charged to the slot that answered"


class TestUsageIsCountedOnceUnderTheProvidersKey:
    def test_groq_usage_and_cost_land_on_its_slot(self, monkeypatch):
        kv = _KV()
        monkeypatch.setattr("app.core.redis_client.get_redis", lambda: kv)
        meta = LLMResponse(text="x", provider="groq", model="m", total_tokens=1000)
        LLMRouter()._log_and_track("explain", meta, "groq_70b")
        today = _utc_today()
        assert kv.get(f"llm:requests:groq_70b:{today}") == "1"
        assert kv.get(f"llm:tokens:groq_70b:{today}") == "1000"
        assert int(kv.get(f"llm:cost_mc:groq_70b:{today}") or 0) > 0, "Groq cost was always $0"
        assert kv.get(f"llm:requests:groq:{today}") is None

    def test_a_gemini_call_is_counted_once(self, monkeypatch):
        """The provider and the router both counted it; usage read double."""
        from app.ai.providers import gemini

        kv = _KV()
        monkeypatch.setattr("app.core.redis_client.get_redis", lambda: kv)
        assert not hasattr(gemini.GeminiProvider, "_track")
        meta = LLMResponse(text="x", provider="gemini", model="m", total_tokens=1000)
        LLMRouter()._log_and_track("explain", meta, "gemini")
        assert kv.get(f"llm:requests:gemini:{_utc_today()}") == "1"


class TestRoutingReadsTheTokenQuota:
    def test_tokens_near_the_limit_count_as_high_usage(self, monkeypatch):
        from app.ai.routing_policy import DAILY_LIMITS

        kv = _KV()
        monkeypatch.setattr("app.core.redis_client.get_redis", lambda: kv)
        today = _utc_today()
        kv.set(f"llm:tokens:groq_70b:{today}", DAILY_LIMITS["groq_70b"]["tokens"] - 1000)
        kv.set(f"llm:requests:groq_70b:{today}", 30)
        assert LLMRouter()._usage_pct("groq_70b") > 0.9


def test_the_counter_day_is_utc():
    assert _utc_today() == datetime.datetime.now(datetime.timezone.utc).date().isoformat()


class TestJsonExtraction:
    def test_a_brace_inside_a_string_after_a_preamble(self):
        from app.ai.providers.base import _extract_json

        text = 'Here is the review:\n{"summary": "s", "issues": [{"code": "if (x) {", "line": 3}]}\nThanks!'
        assert _extract_json(text) == {"summary": "s", "issues": [{"code": "if (x) {", "line": 3}]}

    def test_fenced_with_trailing_text(self):
        from app.ai.providers.base import _extract_json

        text = '```json\n{"a": "}"}\n```\nDone.'
        assert _extract_json(text) == {"a": "}"}

    def test_never_returns_a_non_dict(self):
        from app.ai.providers.base import _extract_json

        for text in ("null", "42", "[1, 2]", '"s"'):
            assert _extract_json(text) == {"raw": text}

    def test_the_first_object_wins(self):
        from app.ai.providers.base import _extract_json

        assert _extract_json('x {"a": 1} y {"b": 2}') == {"a": 1}


class TestAudit2Remainder:
    def test_url_userinfo_is_redacted(self):
        from app.core.redaction import redact_secrets

        out = redact_secrets("Cannot reach Ollama at http://admin:s3cretpw@gpu.local:11434 — is it running?")
        assert "s3cretpw" not in out and "admin" not in out and "gpu.local" in out

    def test_the_gatekeeper_timeout_is_honoured(self, monkeypatch):
        """Ollama forced max(timeout, 120), so GATEKEEPER_TIMEOUT=8 waited 120s."""
        from unittest.mock import patch

        from app.ai.circuit_breaker import reset_breakers
        from app.ai.providers.ollama import OllamaProvider

        monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434")
        reset_breakers()
        with patch("app.ai.providers.ollama.http_requests.post", side_effect=RuntimeError("x")) as post:
            OllamaProvider(min_timeout=0).call_raw(system="s", user="u", max_tokens=8, temperature=0.0, timeout=5)
            OllamaProvider().call_raw(system="s", user="u", max_tokens=8, temperature=0.0, timeout=5)
        assert [c.kwargs["timeout"] for c in post.call_args_list] == [5, 120]

    def test_a_config_error_clears_when_that_provider_answers(self, monkeypatch):
        from app.ai.router import LLMRouter

        monkeypatch.setattr("app.core.redis_client.get_redis", lambda: _KV())
        r = LLMRouter()
        r._note_configuration_error("CONFIG_ERROR: the provider rejected GROQ_API_KEY (401)", "groq_70b")
        assert r.configuration_error()
        r._log_and_track("explain", LLMResponse(text="x", provider="groq", model="m", total_tokens=1), "groq_8b")
        assert r.configuration_error(), "a different provider answering proves nothing"
        r._log_and_track("explain", LLMResponse(text="x", provider="groq", model="m", total_tokens=1), "groq_70b")
        assert r.configuration_error() == ""

    def test_validators_survive_non_string_fields_and_map_severity_up(self):
        from app.ai.validator import validate_issue_triage, validate_pr_analysis

        assert validate_pr_analysis({"risk_level": 2, "summary": "s"})["risk_measured"] is False
        out = validate_pr_analysis({"risk_level": "critical", "summary": "s"})
        assert out["risk_level"] == "high" and out["risk_measured"] is True
        assert validate_issue_triage({"priority": ["high"], "needs_info": "false", "type": "bug"})["needs_info"] is False

    def test_quality_floor_refuses_a_small_substitute(self, monkeypatch):
        from app.ai import model_catalog

        monkeypatch.setenv("LLM_QUALITY_FLOOR", "high")
        monkeypatch.setattr(model_catalog, "best_model", lambda *a, **k: "openai/gpt-oss-20b")
        monkeypatch.setattr(model_catalog, "autoheal_enabled", lambda: True)
        model_catalog._substitutions.clear()
        assert model_catalog.substitute("groq_70b", "groq", "quality", "openai/gpt-oss-120b") == ""


def test_an_invented_file_is_not_plausible_because_of_a_shared_suffix():
    from app.ai.hallucination import _is_plausible_file

    known = {"app/oauth.py", "app/core/auth.py"}
    assert _is_plausible_file("core/auth.py", known)
    assert _is_plausible_file("auth.py", known)
    assert not _is_plausible_file("auth.py", {"app/oauth.py"})
    assert not _is_plausible_file("handlers/auth.py", known)


def test_local_only_doctor_contacts_no_cloud_catalogue(monkeypatch):
    from unittest.mock import patch

    from app.ai.router import model_configuration_report

    monkeypatch.setenv("LLM_LOCAL_ONLY", "1")
    with patch("app.ai.model_catalog.available_models") as fetch:
        report = model_configuration_report()
    fetch.assert_not_called()
    assert "LLM_LOCAL_ONLY" in report["skipped"]
