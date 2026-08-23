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

from app.ai.providers.groq import (
    CONFIG_ERROR_PREFIX,
    GroqProvider,
    _client_error_detail,
    is_configuration_error,
)


def _response(status, body=None):
    r = MagicMock()
    r.status_code = status
    r.headers = {}
    r.json.return_value = body if body is not None else {}
    return r


class TestErrorClassification:
    def test_model_not_found_names_the_model_and_the_fix(self):
        detail = _client_error_detail(
            _response(404, {"error": {"code": "model_not_found"}}), 404, "retired-70b"
        )
        assert is_configuration_error(detail)
        assert "retired-70b" in detail
        assert "LLM_PRIMARY_MODEL" in detail
        assert "not an outage" in detail

    def test_a_404_without_a_body_is_still_diagnosed(self):
        r = _response(404)
        r.json.side_effect = ValueError("no body")
        detail = _client_error_detail(r, 404, "gone")
        assert is_configuration_error(detail)
        assert "gone" in detail

    def test_a_rejected_key_is_reported_as_a_key_problem(self):
        detail = _client_error_detail(_response(401), 401, "any-model")
        assert is_configuration_error(detail)
        assert "GROQ_API_KEY" in detail
        assert "regenerate" in detail.lower()

    def test_a_403_mentions_model_access(self):
        detail = _client_error_detail(_response(403), 403, "gated-model")
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
