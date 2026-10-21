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
    # A clean review names the file it reviewed and finds nothing. This was
    # `"files": []` — no file reviewed at all — which used to pass as clean.
    good = {
        "type": "bugfix", "risk_level": "low", "risk_reason": "small", "summary": "s",
        "review_focus": [], "confidence": 0.9, "has_gaps": False,
        "files": [{"file": "app/x.py", "summary": "Adds a guard before the division.", "issues": []}],
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


def test_a_review_that_names_no_file_is_reported_not_silent():
    """Round-4 audit M1: an answer naming no real file made the code-review
    section vanish with no notice, reading the same as a clean review."""
    answer = {
        "type": "bugfix", "risk_level": "low", "risk_reason": "small", "summary": "s",
        "review_focus": [], "confidence": 0.9, "has_gaps": False,
        "files": [{"file": "lib/y.py", "summary": "s", "issues": []}],  # not a file in the PR
    }
    body, _ = _run(lambda *a, **k: (answer, META), lambda *a, **k: ("Adds a branch.", META))
    assert "code review — no usable answer came back for any file sent" in body


def test_a_grounded_critical_finding_raises_the_recorded_risk(fake_redis):
    """Round-4 audit H1: the risk came from the analysis alone, so a PR the
    review flagged for a critical bug stayed "low" for auto-merge."""
    from app.core.guardrails import get_pr_risk

    answer = {
        "type": "bugfix", "risk_level": "low", "risk_reason": "small", "summary": "s",
        "review_focus": [], "confidence": 0.9, "has_gaps": False,
        "files": [{"file": "app/x.py", "summary": "Divides by zero.", "issues": [{
            "severity": "critical", "line": "4", "code": "return a/0",
            "issue": "Always raises ZeroDivisionError for truthy a", "fix": "return a",
        }]}],
    }
    body, _ = _run(lambda *a, **k: (answer, META), lambda *a, **k: ("Adds a branch.", META))
    assert "Risk raised to HIGH" in body
    pr = {"number": 7, "head": {"sha": "abc123"}, "base": {"repo": {"full_name": "o/r"}}}
    assert get_pr_risk(pr) == "high"


def test_a_bare_filename_is_resolved_to_the_prs_path():
    """Round-4 audit M1: "x.py" for "app/x.py" skipped the entry — and the
    critical finding in it — as an invented file."""
    answer = {
        "type": "bugfix", "risk_level": "low", "risk_reason": "small", "summary": "s",
        "review_focus": [], "confidence": 0.9, "has_gaps": False,
        "files": [{"file": "x.py", "summary": "Divides by zero.", "issues": [{
            "severity": "critical", "line": "4", "code": "return a/0",
            "issue": "Always raises ZeroDivisionError", "fix": "return a",
        }]}],
    }
    body, _ = _run(lambda *a, **k: (answer, META), lambda *a, **k: ("Adds a branch.", META))
    assert "1 critical" in body


def test_a_cut_off_gaps_answer_is_not_no_gaps():
    """An inner gap object parsed out of a truncated answer has no has_gaps,
    and was read as "no gaps"."""
    import app.handlers.pull_request.gaps as G
    from app.handlers.pull_request.errors import SectionUnavailable

    cfg = MagicMock()
    cfg.get.side_effect = lambda *a, **k: k.get("default", True)
    files = FILES + [{"filename": "tests/test_x.py", "patch": "+def test_f(): pass"}]
    inner = {"file": "app/x.py", "function": "f", "risk": "high"}
    with patch("app.ai.router.router.ask", return_value=(inner, META)), \
         patch("app.handlers.pull_request.gaps._in_repository_tests", return_value=False), \
         pytest.raises(SectionUnavailable):
        G._detect_test_gaps(_payload()["pull_request"], "o/r", 7, files, "t", cfg, MagicMock())


def test_a_finding_with_no_text_is_not_rendered():
    """A finding whose text was empty (or stripped as the prompt's example)
    rendered as a bare "**MINOR** ...:  → ``" line."""
    answer = {
        "type": "bugfix", "risk_level": "low", "risk_reason": "small", "summary": "s",
        "review_focus": [], "confidence": 0.9, "has_gaps": False,
        "files": [{"file": "app/x.py", "summary": "Divides by zero.", "issues": [
            {"severity": "minor", "line": "", "code": "", "issue": "", "fix": ""},
            {"severity": "major", "line": "", "code": "", "issue": "Unreachable return", "fix": ""},
        ]}],
    }
    body, _ = _run(lambda *a, **k: (answer, META), lambda *a, **k: ("Adds a branch.", META))
    assert "**MINOR**" not in body
    assert "Unreachable return" in body
    assert "→ ``" not in body


def test_the_summary_does_not_echo_the_prompts_numbered_asks():
    """The model repeated "1. What this PR accomplishes" as headings and the
    summary was published with them."""
    echoed = (
        "1. What this PR accomplishes\nMakes f return None for falsy input.\n"
        "**Key technical changes made:** Adds a branch in f."
    )
    body, _ = _run(lambda *a, **k: ({"raw": "not json"}, META), lambda *a, **k: (echoed, META))
    assert "What this PR accomplishes" not in body
    assert "Key technical changes made" not in body
    assert "Makes f return None for falsy input." in body
    assert "Adds a branch in f." in body


def test_a_summary_starting_with_the_title_is_not_cut():
    """Only the fixed instructions are filtered, never text that repeats the
    PR's own title or body (which are in the prompt too)."""
    from app.handlers.pull_request.analysis import _SUMMARY_ASK, drop_echoed_instructions

    text = "Fix f so that it no longer divides by zero."
    assert drop_echoed_instructions(text, _SUMMARY_ASK) == text


def test_a_summary_that_only_echoes_the_asks_says_so():
    """Not "the model returned nothing": it returned the instructions."""
    echoed = "1. What this PR accomplishes\n2. Key technical changes made"
    body, _ = _run(lambda *a, **k: ({"raw": "not json"}, META), lambda *a, **k: (echoed, META))
    assert "summary — the model only repeated the instructions back" in body
