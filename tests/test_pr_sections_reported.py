"""
tests/test_pr_sections_reported.py

A PR report section that produced nothing used to be left out. On a push the
report was rewritten without it, so the previous commit's findings vanished
as though fixed, and a review that never happened read the same as one that
found nothing. Driven through handle() with the real section code; only the
model and GitHub are stubbed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import app.handlers.pull_request as prh
from app.ai.circuit_breaker import AllProvidersDown
from app.core.config import Config

FILES = [{
    "filename": "app/x.py", "status": "modified", "additions": 3, "deletions": 1,
    "patch": "@@ -1,2 +1,4 @@\n def f(a):\n-    return a\n+    if a:\n+        return a/0\n+    return None\n",
}]


def _payload(action="synchronize"):
    return {
        "action": action, "repository": {"full_name": "o/r"}, "installation": {"id": 1},
        "pull_request": {"number": 7, "title": "fix f", "body": "", "user": {"login": "dev"},
                         "head": {"sha": "abc123"}, "base": {"ref": "main"}},
    }


def _run(ask, ask_text):
    written = {}

    def write(repo, n, tok, marker, body):
        written["body"] = body
        return {"id": 1}

    with patch.object(prh, "get_installation_token", return_value="t"), \
         patch.object(prh, "load_config", return_value=Config({})), \
         patch.object(prh, "gh_get", return_value={"archived": False}), \
         patch("app.github.helpers.pr_files", return_value=FILES), \
         patch("app.intelligence.retrieval.get_context_for_pr", return_value=""), \
         patch("app.ai.gatekeeper.is_substantive", return_value=(True, "")), \
         patch("app.ai.router.router.ask", side_effect=ask), \
         patch("app.ai.router.router.ask_text", side_effect=ask_text), \
         patch("app.handlers.pull_request.review._posted_findings", return_value=set()), \
         patch("app.handlers.pull_request.gaps._in_repository_tests", return_value=False), \
         patch.object(prh, "update_sticky_if_present", side_effect=write), \
         patch.object(prh, "upsert_sticky", side_effect=write):
        try:
            prh.handle(_payload())
            raised = None
        except Exception as e:
            raised = e
    return written.get("body", ""), raised


META = MagicMock(total_tokens=0)


def test_an_unusable_answer_is_reported_by_section_on_a_push():
    truncated = {"raw": '{"files": [{"file": "app/x.py", "issues": [{"severity": "crit'}
    body, raised = _run(lambda *a, **k: (truncated, META), lambda *a, **k: ("Adds a branch.", META))

    assert raised is None, "a bad answer is not retried"
    notice = body.split("<details>")[0]  # unfolded, above every collapsed section
    assert "Not run on this commit" in notice
    for section in ("analysis", "code review", "test gaps"):
        assert f"{section} — the model's answer could not be used" in notice


def test_a_provider_outage_in_the_summary_is_listed_and_retried():
    def down(*a, **k):
        raise AllProvidersDown()

    body, raised = _run(lambda *a, **k: ({"raw": "not json"}, META), down)

    assert isinstance(raised, AllProvidersDown), "an outage passes; the queue retries"
    assert "summary — the AI providers were unavailable" in body


def test_a_clean_answer_reports_nothing_missing():
    good = {
        "type": "bugfix", "risk_level": "low", "risk_reason": "small", "summary": "s",
        "review_focus": [], "confidence": 0.9, "files": [], "has_gaps": False,
    }
    body, raised = _run(lambda *a, **k: (good, META), lambda *a, **k: ("Adds a branch.", META))
    assert raised is None
    assert "Not run on this commit" not in body


@pytest.mark.parametrize("module,func", [
    ("app.handlers.pull_request.analysis", "_build_pr_summary"),
    ("app.handlers.pull_request.gaps", "_detect_test_gaps"),
])
def test_no_section_swallows_an_outage(module, func):
    """Both caught every exception and returned "", so handle() never knew."""
    import importlib

    mod = importlib.import_module(module)
    files = FILES + [{"filename": "tests/test_x.py", "patch": "+def test_f(): pass"}]
    cfg = MagicMock()
    cfg.get.side_effect = lambda *a, **k: k.get("default", True)
    with patch("app.ai.router.router.ask", side_effect=AllProvidersDown()), \
         patch("app.ai.router.router.ask_text", side_effect=AllProvidersDown()), \
         patch("app.handlers.pull_request.gaps._in_repository_tests", return_value=False), \
         pytest.raises(AllProvidersDown):
        getattr(mod, func)(_payload()["pull_request"], "o/r", 7, files, "t", cfg, MagicMock())
