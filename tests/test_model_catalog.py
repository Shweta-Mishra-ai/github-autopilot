"""
tests/test_model_catalog.py

The catalogue exists because a hardcoded model id is a dated claim about
someone else's product, and this deployment paid for that: the provider retired
every Llama chat model, every AI command returned 404 for six days, and the fix
was a string nobody knew to change.

The fixtures in `fixtures_model_catalog.json` are the providers' REAL
catalogues, captured from a live run on 2026-08-29 (Evals run #11). Testing
selection against invented model lists would prove the ranking matches my
imagination rather than the thing it has to work against.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import app.ai.model_catalog as mc

_FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures_model_catalog.json").read_text(encoding="utf-8")
)
GROQ_LIVE = _FIXTURES["groq"]
OPENROUTER_FREE_LIVE = _FIXTURES["openrouter_free"]


@pytest.fixture(autouse=True)
def _clean():
    mc.clear_cache()
    mc.clear_substitutions()
    yield
    mc.clear_cache()
    mc.clear_substitutions()


def _catalog(models, provider="groq"):
    return patch.object(mc, "available_models", lambda p, **k: models if p == provider else [])


class TestSelectionAgainstTheRealCatalogue:

    def test_groq_picks_what_a_human_picked(self):
        """
        The strongest signal available that the ranking is sane: run it against
        the live catalogue and it independently arrives at the two ids an
        operator chose by reading the same list.
        """
        with _catalog(GROQ_LIVE):
            assert mc.best_model("groq", "quality") == "openai/gpt-oss-120b"
            assert mc.best_model("groq", "speed") == "openai/gpt-oss-20b"

    def test_the_newer_version_in_a_family_wins(self):
        """qwen3.6 and qwen3.8 are both served. Picking 3.6 would mean the
        tie-break sorts ascending, which is how a catalogue-driven choice
        silently rots back into an old model."""
        remaining = [m for m in GROQ_LIVE if "gpt-oss" not in m]
        with _catalog(remaining):
            assert mc.best_model("groq", "quality") == "qwen/qwen3.8-27b"

    def test_non_chat_models_are_never_selected(self):
        """Whisper, TTS, prompt-guard and safeguard are all served by this
        provider. Picking one produces a 400 that reads like an outage rather
        than like a bad choice — the worst way to fail."""
        never = [m for m in GROQ_LIVE if mc._EXCLUDE.search(m)]
        assert never, "fixture should contain non-chat models"
        with _catalog(GROQ_LIVE):
            for tier in ("quality", "speed"):
                assert mc.best_model("groq", tier) not in never

    def test_a_narrow_model_is_the_last_resort_not_the_default(self):
        """allam-2-7b is a real chat model, and the wrong one for reviewing
        code in English. It should only surface when nothing else is left."""
        with _catalog(GROQ_LIVE):
            assert mc.best_model("groq", "quality") != "allam-2-7b"
        with _catalog(["allam-2-7b"]):
            assert mc.best_model("groq", "quality") == "allam-2-7b"

    def test_openrouter_only_ever_picks_a_free_model(self):
        """This is the emergency fallback. Reaching it must never start a bill."""
        with _catalog(OPENROUTER_FREE_LIVE + ["anthropic/claude-opus-latest"], "openrouter"):
            for tier in ("quality", "speed"):
                assert mc.best_model("openrouter", tier).endswith(":free")

    def test_openrouter_pick_matches_the_shipped_default(self):
        """One story, not two: the static default and the catalogue's own pick
        must agree, or the fallback silently changes model the first time the
        catalogue is read."""
        from app.ai.providers.openrouter import DEFAULT_MODEL

        with _catalog(OPENROUTER_FREE_LIVE, "openrouter"):
            assert mc.best_model("openrouter", "quality") == DEFAULT_MODEL

    def test_the_shipped_defaults_are_actually_served(self):
        """The bug this whole module exists for: an id in the code that the
        provider does not serve. Checked against the live catalogues."""
        from app.ai.providers.openrouter import DEFAULT_MODEL
        from app.ai.router import DEFAULT_FALLBACK_MODEL, DEFAULT_PRIMARY_MODEL

        assert DEFAULT_PRIMARY_MODEL in GROQ_LIVE
        assert DEFAULT_FALLBACK_MODEL in GROQ_LIVE
        assert DEFAULT_MODEL in OPENROUTER_FREE_LIVE


class TestAnUnreadableCatalogueChangesNothing:
    """
    Every failure here must leave the caller with the id it already had. A
    catalogue lookup that guessed on no information could take a WORKING
    deployment down, which would be strictly worse than the bug it fixes.
    """

    def test_no_catalogue_means_no_opinion(self):
        with _catalog([]):
            assert mc.best_model("groq", "quality") == ""
            assert mc.substitute("groq_70b", "groq", "quality", "anything") == ""

    def test_a_network_failure_is_not_an_empty_catalogue(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "k")
        with patch("requests.get", side_effect=OSError("boom")):
            assert mc.available_models("groq") == []

    def test_a_non_200_is_not_an_empty_catalogue(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "k")
        r = MagicMock(status_code=500)
        with patch("requests.get", return_value=r):
            assert mc.available_models("groq") == []

    def test_a_missing_key_does_not_call_out(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        with patch("requests.get") as get:
            assert mc.available_models("groq") == []
        assert not get.called, "no key means no request, not a request that fails"

    def test_the_only_candidate_being_the_failed_one_yields_nothing(self):
        with _catalog(["openai/gpt-oss-120b"]):
            assert mc.substitute("groq_70b", "groq", "quality", "openai/gpt-oss-120b") == ""

    def test_autoheal_can_be_switched_off(self, monkeypatch):
        monkeypatch.setenv("LLM_MODEL_AUTOHEAL", "0")
        with _catalog(GROQ_LIVE):
            assert mc.substitute("groq_70b", "groq", "quality", "gone") == ""


class TestSubstitutionIsRememberedAndVisible:

    def test_effective_model_returns_the_substitute(self):
        with _catalog(GROQ_LIVE):
            mc.substitute("groq_70b", "groq", "quality", "retired-model")
        assert mc.effective_model("groq_70b", "retired-model") == "openai/gpt-oss-120b"
        # Untouched providers are unaffected.
        assert mc.effective_model("groq_8b", "openai/gpt-oss-20b") == "openai/gpt-oss-20b"

    def test_it_is_reported_not_silent(self):
        with _catalog(GROQ_LIVE):
            mc.substitute("groq_70b", "groq", "quality", "retired-model")
        active = mc.active_substitutions()
        assert active["groq_70b"]["from"] == "retired-model"
        assert active["groq_70b"]["to"] == "openai/gpt-oss-120b"


class TestOnlyAMissingModelIsRepaired:
    """
    Substituting a model for a rejected key would swap a precise "your key is
    invalid" for a baffling "we quietly changed model and it still failed".
    """

    @staticmethod
    def _resp(payload):
        r = MagicMock()
        r.json.return_value = payload
        return r

    @pytest.mark.parametrize("status", [401, 403])
    def test_a_credential_fault_is_never_a_model_fault(self, status):
        from app.ai.providers.base import is_model_missing

        assert not is_model_missing(self._resp({"error": {"code": "model_not_found"}}), status)

    def test_404_is_a_model_fault(self):
        from app.ai.providers.base import is_model_missing

        assert is_model_missing(self._resp({}), 404)

    @pytest.mark.parametrize(
        "message",
        ["The model does not exist", "model has been decommissioned", "No endpoints found"],
    )
    def test_the_body_is_read_not_just_the_status(self, message):
        """Providers disagree about which status a retired model gets; Groq
        answered 400 with `model_decommissioned` at one point."""
        from app.ai.providers.base import is_model_missing

        assert is_model_missing(self._resp({"error": {"message": message}}), 400)

    def test_an_unrelated_400_is_left_alone(self):
        from app.ai.providers.base import is_model_missing

        assert not is_model_missing(self._resp({"error": {"message": "context too long"}}), 400)


class TestTheProvidersActuallyRepairThemselves:
    """
    The end-to-end behaviour: a provider that answers "that model does not
    exist" gets one retry on a model it does serve, and the request succeeds
    instead of becoming a six-day outage.
    """

    @staticmethod
    def _ok(payload=None):
        r = MagicMock()
        r.status_code = 200
        r.headers = {}
        r.json.return_value = payload or {
            "choices": [{"message": {"content": "the answer"}}],
            "usage": {"total_tokens": 5},
        }
        r.raise_for_status.return_value = None
        return r

    @staticmethod
    def _gone(payload=None):
        r = MagicMock()
        r.status_code = 404
        r.headers = {}
        r.json.return_value = payload or {
            "error": {"code": "model_not_found", "message": "The model does not exist"}
        }
        r.raise_for_status.return_value = None
        return r

    def test_groq_retries_once_on_a_served_model(self, monkeypatch):
        import app.ai.circuit_breaker as cb
        from app.ai.providers.groq import GroqProvider

        monkeypatch.setenv("GROQ_API_KEY", "k")
        breaker = MagicMock()
        breaker.is_available.return_value = True
        tried = []

        def post(url, headers=None, json=None, timeout=None):
            tried.append(json["model"])
            return self._gone() if json["model"] == "retired-model" else self._ok()

        with (
            _catalog(GROQ_LIVE),
            patch.object(cb, "get_breaker", return_value=breaker),
            patch("app.ai.providers.groq.http_requests.post", side_effect=post),
        ):
            out = GroqProvider("retired-model", provider_key="groq_70b").call_raw(
                "s", "u", 10, 0.2, 5
            )

        assert tried == ["retired-model", "openai/gpt-oss-120b"]
        assert out.error is None and out.text
        # The response must name the model that actually answered, or every
        # log line and every disclosure footer is a lie.
        assert out.model == "openai/gpt-oss-120b"

    def test_the_fallback_tier_is_replaced_with_a_cheap_model(self, monkeypatch):
        """A substitution must stay in its tier: healing the 20B fallback onto
        the 120B primary would quietly triple the cost of every fallback."""
        import app.ai.circuit_breaker as cb
        from app.ai.providers.groq import GroqProvider

        monkeypatch.setenv("GROQ_API_KEY", "k")
        breaker = MagicMock()
        breaker.is_available.return_value = True
        tried = []

        def post(url, headers=None, json=None, timeout=None):
            tried.append(json["model"])
            return self._gone() if json["model"] == "retired-small" else self._ok()

        with (
            _catalog(GROQ_LIVE),
            patch.object(cb, "get_breaker", return_value=breaker),
            patch("app.ai.providers.groq.http_requests.post", side_effect=post),
        ):
            GroqProvider("retired-small", provider_key="groq_8b").call_raw("s", "u", 10, 0.2, 5)

        assert tried[-1] == "openai/gpt-oss-20b"

    def test_a_rejected_key_is_reported_not_worked_around(self, monkeypatch):
        import app.ai.circuit_breaker as cb
        from app.ai.providers.base import is_configuration_error
        from app.ai.providers.groq import GroqProvider

        monkeypatch.setenv("GROQ_API_KEY", "k")
        breaker = MagicMock()
        breaker.is_available.return_value = True
        tried = []

        def post(url, headers=None, json=None, timeout=None):
            tried.append(json["model"])
            r = MagicMock()
            r.status_code = 401
            r.headers = {}
            r.json.return_value = {"error": {"code": "invalid_api_key"}}
            return r

        with (
            _catalog(GROQ_LIVE),
            patch.object(cb, "get_breaker", return_value=breaker),
            patch("app.ai.providers.groq.http_requests.post", side_effect=post),
        ):
            out = GroqProvider("openai/gpt-oss-120b", provider_key="groq_70b").call_raw(
                "s", "u", 10, 0.2, 5
            )

        assert len(tried) == 1, "a bad key must not trigger a model retry"
        assert is_configuration_error(out.error)
        assert "GROQ_API_KEY" in out.error
        assert mc.active_substitutions() == {}

    def test_openrouter_repairs_and_the_retry_uses_the_new_model(self, monkeypatch):
        """The body is built before the substitution, so a retry that forgot to
        rebuild it would re-send the dead id and look like the fix failed."""
        import app.ai.providers.openrouter as orx

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
        breaker = MagicMock()
        breaker.is_available.return_value = True
        tried = []

        def post(url, headers=None, json=None, timeout=None):
            tried.append(json["model"])
            if json["model"] == "mistralai/mistral-7b-instruct:free":
                return self._gone({"error": {"code": 404, "message": "No endpoints found"}})
            return self._ok({"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {}})

        with (
            _catalog(OPENROUTER_FREE_LIVE, "openrouter"),
            patch.object(orx, "get_breaker", return_value=breaker),
            patch.object(orx.http_requests, "post", side_effect=post),
        ):
            result, meta = orx.OpenRouterProvider("mistralai/mistral-7b-instruct:free").ask("s", "u")

        assert tried == [
            "mistralai/mistral-7b-instruct:free",
            "nvidia/nemotron-3-super-120b-a12b:free",
        ]
        assert result == {"ok": True}
        assert meta.error is None


class TestTheSuiteNeverReachesAProvider:
    """
    OpenRouter's catalogue needs no API key, so the moment providers learned to
    repair a retired model id, any test feeding a 404 reached openrouter.ai for
    real. CI caught it; locally it passed only because the sandbox blocks that
    host. A test whose result depends on the network is not a test.
    """

    def test_the_catalogue_is_stubbed_by_default(self):
        """The autouse guard in conftest, asserted rather than assumed."""
        assert mc.available_models("openrouter") == []
        assert mc.available_models("groq") == []

    def test_no_http_call_escapes_during_a_provider_404(self, monkeypatch):
        import app.ai.circuit_breaker as cb
        from app.ai.providers.groq import GroqProvider

        monkeypatch.setenv("GROQ_API_KEY", "k")
        breaker = MagicMock()
        breaker.is_available.return_value = True
        gone = MagicMock()
        gone.status_code = 404
        gone.headers = {}
        gone.json.return_value = {"error": {"code": "model_not_found"}}

        with (
            patch("requests.get") as outbound,
            patch.object(cb, "get_breaker", return_value=breaker),
            patch("app.ai.providers.groq.http_requests.post", return_value=gone),
        ):
            GroqProvider("retired", provider_key="groq_70b").call_raw("s", "u", 10, 0.2, 5)

        assert not outbound.called, "a unit test reached out to a provider catalogue"
