"""
tests/test_model_disclosure.py

The README says every comment discloses which model wrote it. Only slash-
command replies did, and they named only the LAST call's model.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from app.ai import router as R
from app.ai.providers.base import LLMResponse


def _answered(model, provider="groq"):
    R.router._log_and_track("t", LLMResponse(text="", provider=provider, model=model), "groq_70b")


def test_every_model_of_the_event_is_named_in_order():
    R.reset_last_call()
    _answered("openai/gpt-oss-120b")
    _answered("gemini-2.5-flash", "gemini")
    _answered("openai/gpt-oss-120b")
    assert R.last_model_disclosure() == " · model: `openai/gpt-oss-120b`, `gemini-2.5-flash`"


def test_a_reset_starts_a_new_event():
    _answered("x")
    R.reset_last_call()
    assert R.last_model_disclosure() == ""


def test_the_pr_report_names_the_models():
    from unittest.mock import patch

    from app.handlers import pull_request as pr_mod

    cfg = MagicMock()
    cfg.pr_enabled.return_value = True
    cfg.get.side_effect = lambda *a, **k: k.get("default", True)
    cfg.footer = ""
    payload = {"action": "opened", "repository": {"full_name": "o/r"}, "installation": {"id": 1},
               "pull_request": {"number": 2, "title": "t", "body": "", "user": {"login": "a"},
                                "head": {"sha": "s"}, "base": {"ref": "main"}}}

    def analyse(*a, **k):
        _answered("openai/gpt-oss-120b")
        return "analysis"

    with patch.object(pr_mod, "get_installation_token", return_value="t"), \
         patch.object(pr_mod, "load_config", return_value=cfg), \
         patch.object(pr_mod, "gh_get", return_value={}), \
         patch("app.github.helpers.pr_files", return_value=[{"filename": "a.py", "patch": "+x"}]), \
         patch.object(pr_mod, "_analyze_pr", side_effect=analyse), \
         patch.object(pr_mod, "_build_pr_summary", return_value="s"), \
         patch.object(pr_mod, "_review_code", return_value=("", [])), \
         patch.object(pr_mod, "_detect_test_gaps", return_value=""), \
         patch.object(pr_mod, "notify_pr_opened"), \
         patch.object(pr_mod, "upsert_sticky") as upsert:
        pr_mod.handle(payload)
    assert "*Written with AI · model: `openai/gpt-oss-120b`*" in upsert.call_args[0][4]
