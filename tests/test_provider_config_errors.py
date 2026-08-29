"""
A provider misconfiguration is not an outage, and treating it as one hides it.

What happened: Groq answered 404 for a retired model id. That fell through to
raise_for_status(), was recorded as a circuit-breaker failure, and after three
requests the breaker opened on the 70b provider and after five more on the 8b
fallback. The router then reported "all providers down" — which sent a
maintainer to check provider status when the fix was one environment variable.

A breaker exists to stop hammering a service that might recover. A model that
does not exist, and a key that is not valid, will not recover on their own, so
opening the breaker only replaces a precise error with a vague one.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.ai.providers.base import (
    CONFIG_ERROR_PREFIX,
    client_error_detail,
    is_configuration_error,
)
from app.ai.providers.groq import GroqProvider


def _response(status, body=None):
    r = MagicMock()
    r.status_code = status
    r.headers = {}
    r.json.return_value = body if body is not None else {}
    return r


class TestErrorClassification:
    def test_model_not_found_names_the_model_and_the_fix(self):
        detail = client_error_detail(
            _response(404, {"error": {"code": "model_not_found"}}),
            404,
            "retired-70b",
            "GROQ_API_KEY",
            "LLM_PRIMARY_MODEL",
        )
        assert is_configuration_error(detail)
        assert "retired-70b" in detail
        assert "LLM_PRIMARY_MODEL" in detail
        assert "not an outage" in detail

    def test_a_404_without_a_body_is_still_diagnosed(self):
        r = _response(404)
        r.json.side_effect = ValueError("no body")
        detail = client_error_detail(r, 404, "gone", "GROQ_API_KEY", "LLM_PRIMARY_MODEL")
        assert is_configuration_error(detail)
        assert "gone" in detail

    def test_a_rejected_key_is_reported_as_a_key_problem(self):
        detail = client_error_detail(_response(401), 401, "any-model", "GROQ_API_KEY")
        assert is_configuration_error(detail)
        assert "GROQ_API_KEY" in detail
        assert "regenerate" in detail.lower()

    def test_the_key_variable_named_is_the_callers(self):
        # gemini and groq report different variables; a hardcoded name would
        # send a Gemini operator to regenerate the wrong key.
        detail = client_error_detail(_response(401), 401, "m", "GEMINI_API_KEY")
        assert "GEMINI_API_KEY" in detail and "GROQ_API_KEY" not in detail

    def test_google_reports_an_invalid_key_as_400(self):
        # Google answers an invalid key with 400 + API_KEY_INVALID rather than
        # 401, so the status alone would misclassify it as a bad request.
        detail = client_error_detail(
            _response(400, {"error": {"status": "INVALID_ARGUMENT", "message": "API_KEY_INVALID"}}),
            400,
            "gemini-1.5-flash",
            "GEMINI_API_KEY",
        )
        assert is_configuration_error(detail)
        assert "GEMINI_API_KEY" in detail

    def test_a_403_mentions_model_access(self):
        detail = client_error_detail(_response(403), 403, "gated-model", "GROQ_API_KEY")
        assert is_configuration_error(detail)
        assert "gated-model" in detail

    @pytest.mark.parametrize("error", ["", None, "Server error 503", "Request timed out"])
    def test_transient_errors_are_not_configuration_errors(self, error):
        assert not is_configuration_error(error)


class TestTheBreakerIsLeftAlone:
    """The behaviour that produced the misleading 'all providers down'."""

    @pytest.mark.parametrize("status", [401, 403, 404])
    def test_a_client_error_does_not_record_a_breaker_failure(self, status, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_value")
        provider = GroqProvider("some-model")
        breaker = MagicMock()
        breaker.allow.return_value = True

        with patch("app.ai.circuit_breaker.get_breaker", return_value=breaker), patch(
            "app.ai.providers.groq.cb.get_breaker", return_value=breaker
        ), patch("requests.post", return_value=_response(status)), patch(
            "app.ai.providers.groq.http_requests.post", return_value=_response(status)
        ):
            _text, resp = provider.ask_text("sys", "user", 100, 10)

        assert breaker.record_failure.call_count == 0, (
            f"a {status} is permanent — recording it as a breaker failure opens the "
            f"circuit and reports an outage instead of the misconfiguration"
        )
        assert resp.error.startswith(CONFIG_ERROR_PREFIX)

    def test_a_server_error_still_opens_the_breaker(self, monkeypatch):
        # The guard must not disable the breaker for faults it exists for.
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_value")
        provider = GroqProvider("some-model")
        breaker = MagicMock()
        breaker.allow.return_value = True

        with patch("app.ai.providers.groq.cb.get_breaker", return_value=breaker), patch(
            "app.ai.providers.groq.http_requests.post", return_value=_response(503)
        ):
            _text, resp = provider.ask_text("sys", "user", 100, 10)

        assert breaker.record_failure.called, "a 503 may recover — the breaker is right here"
        assert not is_configuration_error(resp.error)


class TestTheRouterSaysWhatIsWrong:
    def test_a_configuration_fault_replaces_the_try_again_message(self):
        from app.ai.circuit_breaker import AllProvidersDown
        from app.ai.router import LLMRouter

        router = LLMRouter()
        router.clear_configuration_error()
        router._note_configuration_error(f"{CONFIG_ERROR_PREFIX} model `x` is not served")

        with patch.object(LLMRouter, "ask", side_effect=AllProvidersDown()):
            result, meta = router.safe_ask(
                "sys", "user", degraded_message="Providers are busy, try again shortly."
            )

        assert result["_configuration_error"] is True
        assert "not served" in result["message"]
        assert "try again shortly" not in result["message"], (
            "telling someone to wait is wrong when waiting cannot help"
        )
        router.clear_configuration_error()

    def test_a_genuine_outage_keeps_the_try_again_message(self):
        from app.ai.circuit_breaker import AllProvidersDown
        from app.ai.router import LLMRouter

        router = LLMRouter()
        router.clear_configuration_error()

        with patch.object(LLMRouter, "ask", side_effect=AllProvidersDown()):
            result, meta = router.safe_ask(
                "sys", "user", degraded_message="Providers are busy, try again shortly."
            )

        assert result["_configuration_error"] is False
        assert "try again shortly" in result["message"]

    def test_status_reports_the_models_actually_in_use(self, monkeypatch):
        from app.ai.router import LLMRouter

        monkeypatch.setenv("LLM_PRIMARY_MODEL", "primary-x")
        monkeypatch.setenv("LLM_FALLBACK_MODEL", "fallback-y")
        status = LLMRouter().status()
        assert status["models"] == {"primary": "primary-x", "fallback": "fallback-y"}

    def test_status_exposes_the_configuration_fault(self):
        from app.ai.router import LLMRouter

        router = LLMRouter()
        router.clear_configuration_error()
        assert router.status()["configuration_error"] == ""
        router._note_configuration_error(f"{CONFIG_ERROR_PREFIX} bad model")
        assert "bad model" in router.status()["configuration_error"]
        router.clear_configuration_error()


class TestHealthReportsIt:
    """
    The fault is discovered on a webhook thread and read from /health on a
    different request. Open circuit breakers cannot express it: an open
    breaker looks the same whether the provider is having a bad hour or the
    model id is wrong, and only one of those is worth waiting out.
    """

    def _get(self):
        import server

        headers = (
            {"Authorization": f"Bearer {server.METRICS_TOKEN}"} if server.METRICS_TOKEN else {}
        )
        return server.app.test_client().get("/health", headers=headers).get_json()

    def test_models_in_use_are_reported(self):
        from app.ai.router import DEFAULT_PRIMARY_MODEL, LLMRouter

        LLMRouter().clear_configuration_error()
        checks = self._get()["checks"]
        assert checks["llm_models"]["primary"] == DEFAULT_PRIMARY_MODEL
        assert "fallback" in checks["llm_models"]

    def test_no_fault_means_no_configuration_error(self):
        from app.ai.router import LLMRouter

        LLMRouter().clear_configuration_error()
        assert self._get()["checks"]["llm_configuration_error"] == ""

    def test_a_fault_is_visible_and_changes_the_overall_status(self):
        from app.ai.router import LLMRouter

        router = LLMRouter()
        router.clear_configuration_error()
        try:
            router._note_configuration_error(
                f"{CONFIG_ERROR_PREFIX} the provider does not serve the model `llama-x`"
            )
            payload = self._get()
            assert "llama-x" in payload["checks"]["llm_configuration_error"]
            assert payload["status"] == "misconfigured", (
                "'degraded' reads as something that may pass; this will not "
                "improve until someone changes a setting"
            )
        finally:
            router.clear_configuration_error()

    def test_health_survives_a_failure_in_its_own_reporting(self):
        from unittest.mock import patch as _patch

        with _patch("app.ai.router.LLMRouter.status", side_effect=RuntimeError("boom")):
            payload = self._get()
        assert payload["status"] in {"ok", "degraded"}, "health must not fail on its own reporting"
        assert payload["checks"]["llm_configuration_error"] == ""


class TestGeminiHasTheSameProtection:
    """
    The fallback provider had the identical defect: a 400 recorded a breaker
    failure, and 401/403/404 fell through to raise_for_status() into the
    generic handler which recorded one too.

    That is the worse place for it. The fallback is what the router reaches for
    when the primary is already broken, so a misconfigured fallback turns a
    recoverable single-provider fault into "all providers down".
    """

    @staticmethod
    def _resp(status, body=None):
        r = MagicMock()
        r.status_code = status
        r.headers = {}
        r.text = "{}"
        r.json.return_value = body if body is not None else {}
        return r

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    def test_a_client_error_does_not_open_the_breaker(self, status, monkeypatch):
        from app.ai.providers.gemini import GeminiProvider

        monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
        breaker = MagicMock()
        breaker.is_available.return_value = True

        with patch("app.ai.providers.gemini.get_breaker", return_value=breaker), patch(
            "app.ai.providers.gemini.http_requests.post", return_value=self._resp(status)
        ):
            resp = GeminiProvider().call_raw("sys", "user", 100, 0.2, 10)

        assert breaker.record_failure.call_count == 0, (
            f"a {status} is permanent — recording it opens the circuit and turns "
            f"a configuration fault into a reported outage"
        )
        assert is_configuration_error(resp.error)

    def test_a_server_error_still_opens_the_breaker(self, monkeypatch):
        from app.ai.providers.gemini import GeminiProvider

        monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
        breaker = MagicMock()
        breaker.is_available.return_value = True

        with patch("app.ai.providers.gemini.get_breaker", return_value=breaker), patch(
            "app.ai.providers.gemini.http_requests.post", return_value=self._resp(503)
        ):
            resp = GeminiProvider().call_raw("sys", "user", 100, 0.2, 10)

        assert breaker.record_failure.called, "a 503 may recover — the breaker is right here"
        assert not is_configuration_error(resp.error)

    def test_the_model_id_is_overridable_without_a_deploy(self, monkeypatch):
        # It was hardcoded. When a provider retires a model — which is exactly
        # what happened to this deployment's Groq models — a hardcoded id makes
        # the only fix a code change.
        from app.ai.providers.gemini import DEFAULT_GEMINI_MODEL, GeminiProvider, gemini_model

        monkeypatch.delenv("LLM_GEMINI_MODEL", raising=False)
        assert gemini_model() == DEFAULT_GEMINI_MODEL

        monkeypatch.setenv("LLM_GEMINI_MODEL", "gemini-9-future")
        assert gemini_model() == "gemini-9-future"
        assert GeminiProvider().model_name == "gemini-9-future"

    def test_every_response_names_the_model_actually_used(self, monkeypatch):
        # Including the paths that return before any HTTP call — a response
        # naming the default while the override was in use would send the
        # reader to check the wrong model.
        from app.ai.providers.gemini import GeminiProvider

        monkeypatch.setenv("LLM_GEMINI_MODEL", "gemini-9-future")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setattr("app.ai.providers.gemini.GEMINI_API_KEY", "")

        breaker = MagicMock()
        breaker.is_available.return_value = True
        with patch("app.ai.providers.gemini.get_breaker", return_value=breaker):
            resp = GeminiProvider().call_raw("sys", "user", 100, 0.2, 10)
        assert resp.model == "gemini-9-future", "the no-key path must name the real model"

        breaker.is_available.return_value = False
        with patch("app.ai.providers.gemini.get_breaker", return_value=breaker):
            resp = GeminiProvider().call_raw("sys", "user", 100, 0.2, 10)
        assert resp.model == "gemini-9-future", "the open-circuit path must too"


class TestTheTierIsNotGuessedFromTheModelName:
    """
    provider_key decided the budget, the circuit breaker and the quality tier
    by looking for "70b" or "versatile" in the model id.

    That worked only while both models happened to be Llama. The provider
    retired every Llama chat model, and the replacements are
    `openai/gpt-oss-120b` and `openai/gpt-oss-20b` — neither string matches, so
    BOTH providers would have returned `groq_8b`. Primary and fallback would
    have shared one circuit breaker, meaning a primary failure opens the
    breaker on the fallback that exists to cover it, and both would draw on the
    small model's token budget.

    The router knows which tier it is constructing. It says so now.
    """

    def test_the_router_gives_each_provider_its_own_tier(self):
        from app.ai.router import LLMRouter

        router = LLMRouter()
        assert router._groq_70b.provider_key == "groq_70b"
        assert router._groq_8b.provider_key == "groq_8b"
        assert router._groq_70b.provider_key != router._groq_8b.provider_key, (
            "sharing a provider_key means sharing a circuit breaker, so the "
            "fallback is opened by the failure it exists to survive"
        )

    def test_the_tier_survives_a_model_id_with_no_size_marker(self):
        from app.ai.providers.groq import GroqProvider

        # The exact shape that broke it: no "70b", no "versatile".
        primary = GroqProvider("openai/gpt-oss-120b", provider_key="groq_70b")
        fallback = GroqProvider("openai/gpt-oss-20b", provider_key="groq_8b")
        assert primary.provider_key == "groq_70b"
        assert fallback.provider_key == "groq_8b"

    def test_an_explicit_key_is_not_overridden_by_the_name(self):
        from app.ai.providers.groq import GroqProvider

        # A name that WOULD infer the other tier must not win over the caller.
        assert GroqProvider("llama-3.3-70b-versatile", provider_key="groq_8b").provider_key == (
            "groq_8b"
        )

    def test_inference_remains_only_for_callers_that_do_not_say(self):
        from app.ai.providers.groq import GroqProvider

        assert GroqProvider("some-70b-model").provider_key == "groq_70b"
        assert GroqProvider("some-small-model").provider_key == "groq_8b"

    def test_the_defaults_are_ids_the_provider_actually_serves(self):
        # Not a live check — this environment cannot reach the provider. It
        # pins the ids to the list the eval preflight printed from the
        # provider's own API on 2026-08-28 using this deployment's key, so a
        # future edit back to a retired id fails here rather than in production.
        from app.ai.router import DEFAULT_FALLBACK_MODEL, DEFAULT_PRIMARY_MODEL

        served_2026_08_28 = {
            "allam-2-7b",
            "groq/compound",
            "groq/compound-mini",
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
            "openai/gpt-oss-safeguard-20b",
            "qwen/qwen3.6-27b",
            "qwen/qwen3.8-27b",
        }
        for model in (DEFAULT_PRIMARY_MODEL, DEFAULT_FALLBACK_MODEL):
            assert model in served_2026_08_28, (
                f"{model!r} was not in the provider's model list. Every AI command "
                f"fails on a model id the provider does not serve."
            )
        assert DEFAULT_PRIMARY_MODEL != DEFAULT_FALLBACK_MODEL, (
            "a fallback identical to the primary is not a fallback"
        )


class TestTheShippedConfigDoesNotAdvertiseDeadSettings:
    def test_there_is_no_ai_section(self):
        """
        `.ai-repo-manager.yml` carried primary_model and fallback_model that
        nothing read — app/core/config.py says so explicitly. When the provider
        retired the models, that file is the first place someone would edit to
        fix it, and editing it would have done nothing.
        """
        from pathlib import Path

        import yaml

        root = Path(__file__).resolve().parent.parent
        config = yaml.safe_load((root / ".ai-repo-manager.yml").read_text(encoding="utf-8"))
        assert "ai" not in config, (
            "the ai: section is not read by any code path; shipping it invites "
            "an operator to change a setting that has no effect"
        )
