"""
The eval gate must not blame the prompts for a provider outage.

The first scheduled run of the suite returned a 0.0 pass rate and filed an
issue reading "review quality has regressed". The real cause was a 404 from
Groq for a retired model id: every request failed, so every case scored zero
on "no code fence" because there was no output at all. Eleven failing cases
described a prompt problem that did not exist, and the actual fix was one
environment variable.

Same defect class as reporting a missing API key as a quality drop — a
diagnosis that points at the wrong half of the system costs more time than
no diagnosis.
"""

from unittest.mock import MagicMock, patch

import pytest

from evals.run import check_configured_models, configured_models


def _models_response(status=200, ids=("llama-3.3-70b-versatile", "llama-3.1-8b-instant")):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = {"data": [{"id": i} for i in ids]}
    return r


class TestModelCheck:
    def test_all_models_present_is_ok(self, monkeypatch):
        monkeypatch.delenv("LLM_PRIMARY_MODEL", raising=False)
        monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)
        with patch("requests.get", return_value=_models_response()):
            ok, detail = check_configured_models("real-key")
        assert ok is True
        assert "all available" in detail

    def test_every_model_missing_fails_with_the_real_cause(self, monkeypatch):
        monkeypatch.setenv("LLM_PRIMARY_MODEL", "retired-70b")
        monkeypatch.setenv("LLM_FALLBACK_MODEL", "retired-8b")
        with patch("requests.get", return_value=_models_response(ids=("current-model",))):
            ok, detail = check_configured_models("real-key")
        assert ok is False
        assert "not a quality regression" in detail
        assert "retired-70b" in detail and "retired-8b" in detail
        # The operator needs to know what to set it TO, not just that it is wrong.
        assert "current-model" in detail
        assert "LLM_PRIMARY_MODEL" in detail

    def test_the_report_says_production_is_affected_too(self, monkeypatch):
        monkeypatch.setenv("LLM_PRIMARY_MODEL", "gone")
        monkeypatch.setenv("LLM_FALLBACK_MODEL", "gone-too")
        with patch("requests.get", return_value=_models_response(ids=("other",))):
            _, detail = check_configured_models("real-key")
        assert "live bot" in detail, (
            "A 404 for the configured model breaks every AI command in production, "
            "not just this suite. Reporting it as a CI problem understates it."
        )

    def test_one_missing_model_warns_but_still_runs(self, monkeypatch):
        monkeypatch.setenv("LLM_PRIMARY_MODEL", "llama-3.3-70b-versatile")
        monkeypatch.setenv("LLM_FALLBACK_MODEL", "retired-8b")
        with patch("requests.get", return_value=_models_response(ids=("llama-3.3-70b-versatile",))):
            ok, detail = check_configured_models("real-key")
        assert ok is True, "one working model is still worth measuring"
        assert "WARNING" in detail
        assert "retired-8b" in detail

    def test_a_rejected_key_is_reported_as_a_key_problem(self):
        with patch("requests.get", return_value=_models_response(status=401)):
            ok, detail = check_configured_models("bad-key")
        assert ok is False
        assert "401" in detail and "regenerate" in detail.lower()

    @pytest.mark.parametrize("status", [429, 500, 503])
    def test_an_inconclusive_check_does_not_block_the_suite(self, status):
        # The suite itself is the real measurement; refusing to run it over a
        # probe that could not answer would be the worse failure.
        with patch("requests.get", return_value=_models_response(status=status)):
            ok, detail = check_configured_models("real-key")
        assert ok is True
        assert "NOTE" in detail

    def test_a_network_error_does_not_raise(self):
        with patch("requests.get", side_effect=OSError("dns")):
            ok, detail = check_configured_models("real-key")
        assert ok is True
        assert "could not check" in detail

    def test_unparseable_body_does_not_raise(self):
        r = MagicMock()
        r.status_code = 200
        r.json.side_effect = ValueError("not json")
        with patch("requests.get", return_value=r):
            ok, detail = check_configured_models("real-key")
        assert ok is True
        assert "NOTE" in detail

    def test_configured_models_follow_the_environment(self, monkeypatch):
        monkeypatch.setenv("LLM_PRIMARY_MODEL", "a")
        monkeypatch.setenv("LLM_FALLBACK_MODEL", "b")
        assert configured_models() == ["a", "b"]

    def test_defaults_come_from_the_router_not_a_copy(self, monkeypatch):
        # If these drift apart the check validates models the bot never calls,
        # reporting success for a configuration that cannot work.
        monkeypatch.delenv("LLM_PRIMARY_MODEL", raising=False)
        monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)
        from app.ai.router import DEFAULT_FALLBACK_MODEL, DEFAULT_PRIMARY_MODEL

        assert configured_models() == [DEFAULT_PRIMARY_MODEL, DEFAULT_FALLBACK_MODEL]


class TestExitCodeContract:
    def test_the_runner_documents_a_distinct_code_for_a_missing_model(self):
        import evals.run as run

        assert "3  the configured model does not exist" in run.__doc__, (
            "The workflow branches on exit code 3 to decide whether to report a "
            "quality regression or an infrastructure failure."
        )

    def test_the_workflow_branches_on_that_code(self):
        from pathlib import Path

        wf = Path(__file__).resolve().parent.parent / ".github/workflows/evals.yml"
        text = wf.read_text(encoding="utf-8")
        assert "exit_code" in text, "the run step must publish its exit code"
        assert 'outputs.exit_code }}\" = \"3\"' in text, (
            "the report step must branch on exit code 3, or a retired model is "
            "reported as a quality regression again"
        )
