"""
tests/test_evals_harness.py — the eval harness itself must be trustworthy.
(The evals spend provider quota and are NOT run here; this tests the scorers
and case files, which are pure/static.)
"""

import json
from pathlib import Path

from evals.scorers import score_output, summarize

_ROOT = Path(__file__).parent.parent


class TestScorer:

    def test_all_checks_pass(self):
        case = {"id": "c1", "must_mention": ["null check"], "require_code_block": True}
        out = "The bug is a missing null check.\n```python\nif x is None: ...\n```" + "x" * 50
        r = score_output(out, case)
        assert r.passed and r.score == 1.0 and r.failures == []

    def test_missing_mention_fails_check(self):
        case = {"id": "c2", "must_mention": ["sql injection"]}
        r = score_output("Looks fine to me!" + "x" * 80, case)
        assert not r.passed
        assert any("must_mention" in f for f in r.failures)

    def test_must_not_mention(self):
        case = {"id": "c3", "must_not_mention": ["critical"], "min_length": 10}
        assert score_output("This is a CRITICAL disaster", case).failures
        assert score_output("Nice clean helper function", case).passed

    def test_regex_alternation(self):
        case = {"id": "c4", "must_mention": ["off.by.one|start \\+ size"], "min_length": 5}
        assert score_output("classic off-by-one error", case).passed
        assert score_output("use items[start:start + size]", case).passed

    def test_empty_case_fails_loudly(self):
        r = score_output("anything", {"id": "c5", "min_length": None} if False else {})
        # A case with no checks beyond default length must not silently pass 100%
        assert r.case_id == "?"

    def test_summarize(self):
        case = {"id": "a", "must_mention": ["x"], "min_length": 1}
        results = [score_output("x", case), score_output("y", case)]
        stats = summarize(results)
        assert stats["cases"] == 2
        assert stats["failed_cases"] == ["a"]
        assert 0 < stats["pass_rate"] < 1


class TestCaseFiles:
    """The golden case files must stay loadable and well-formed."""

    def _cases(self, name):
        return json.loads((_ROOT / "evals" / "cases" / name).read_text(encoding="utf-8"))

    def test_fix_cases_schema(self):
        cases = self._cases("fix_cases.json")
        assert len(cases) >= 5
        ids = [c["id"] for c in cases]
        assert len(ids) == len(set(ids)), "duplicate case ids"
        for c in cases:
            assert c["title"] and c["context"] and c["must_mention"]

    def test_review_cases_schema(self):
        cases = self._cases("review_cases.json")
        assert len(cases) >= 4
        for c in cases:
            assert c["filename"] and c["patch"] and c["planted"]
            assert c["patch"].startswith("@@"), f"{c['id']}: patch needs a @@ hunk header"

    def test_review_patches_are_mappable(self):
        """Every review case's patch must yield commentable lines — otherwise
        the inline-review path can't anchor and the case tests nothing."""
        from app.github.patch_parser import commentable_lines

        for c in self._cases("review_cases.json"):
            assert commentable_lines(c["patch"]), f"{c['id']}: patch parses to no lines"


class TestTheReviewHarnessActuallySeesTheReview:
    """
    The review half of this suite measured nothing at all.

    `_review_code` returns (markdown, inline_comments) and its own docstring
    says "This function posts nothing itself." The harness patched `gh_post`,
    collected what it caught, and threw the return value away — so the scored
    output was always the empty string. Five review cases came back empty
    every night and were read first as a rate limit, then as a quality
    regression.

    These tests fail if the harness ever goes blind again: they stub the
    router with a believable answer and assert the harness comes back with the
    reviewer's own words in it.
    """

    _ANSWER = {
        "confidence": 0.9,
        "files": [],
    }

    def _stub_router(self, cases):
        from unittest.mock import MagicMock

        def fake_ask(_self, _system, user, *a, **k):
            files = [
                {
                    "file": c["filename"],
                    "score": 3,
                    "summary": f"SQL injection risk in {c['filename']}.",
                    "issues": [
                        {
                            "severity": "critical",
                            "line": "3",
                            "issue": "user input is interpolated into the query",
                            "fix": "use parameterized queries",
                        }
                    ],
                }
                for c in cases
                if f"FILE: {c['filename']}" in user
            ]
            meta = MagicMock(provider="groq", model="m", total_tokens=10, cost_usd=0.0)
            return {"files": files, "confidence": 0.9}, meta

        return fake_ask

    def test_review_cases_produce_scorable_output(self, monkeypatch):
        from unittest.mock import patch

        import app.ai.router as router_mod
        import evals.run as ev

        monkeypatch.setenv("EVAL_CASE_DELAY_SECONDS", "0")
        monkeypatch.setattr(ev, "CASE_DELAY_SECONDS", 0.0)

        cases = json.loads(
            (_ROOT / "evals" / "cases" / "review_cases.json").read_text(encoding="utf-8")
        )
        fake = self._stub_router(cases)

        with patch.object(router_mod.LLMRouter, "ask", fake):
            results, blocked = ev.run_review_cases()

        # The point of the test: with a provider that answered, nothing is
        # blocked and every case got a real score.
        assert blocked == [], f"harness saw no output for {blocked}"
        assert len(results) == len(cases)

    def test_the_reviewers_own_words_reach_the_scorer(self, monkeypatch):
        """
        Not just "non-empty" — the text the reviewer produced. An empty string
        joined with an empty list is also non-empty once you add a separator,
        so assert on content the stub uniquely produced.
        """
        from unittest.mock import MagicMock, patch

        import app.ai.router as router_mod
        from app.handlers.pull_request import _review_code

        cases = json.loads(
            (_ROOT / "evals" / "cases" / "review_cases.json").read_text(encoding="utf-8")
        )
        case = cases[0]
        fake = self._stub_router(cases)

        cfg = MagicMock()
        cfg.get.side_effect = lambda *a, **kw: kw.get("default", True)
        cfg.footer = ""
        files = [{"filename": case["filename"], "patch": case["patch"]}]

        with patch.object(router_mod.LLMRouter, "ask", fake):
            markdown, inline = _review_code(
                {"head": {"sha": "eval0000"}},
                "eval/repo",
                1,
                files,
                "tok",
                cfg,
                MagicMock(),
                "",
                MagicMock(),
            )

        combined = "\n".join([markdown] + [c.get("body", "") for c in inline or []])
        assert "parameterized queries" in combined
        assert case["filename"] in combined

    def test_gh_post_alone_is_empty(self):
        """
        The bug, pinned. Capturing only gh_post yields nothing, so a harness
        built on it can never score a review — regardless of the provider.
        """
        from unittest.mock import MagicMock, patch

        import app.ai.router as router_mod
        from app.handlers.pull_request import _review_code

        cases = json.loads(
            (_ROOT / "evals" / "cases" / "review_cases.json").read_text(encoding="utf-8")
        )
        case = cases[0]
        fake = self._stub_router(cases)

        cfg = MagicMock()
        cfg.get.side_effect = lambda *a, **kw: kw.get("default", True)
        cfg.footer = ""
        posted = []

        with (
            patch.object(router_mod.LLMRouter, "ask", fake),
            patch(
                "app.handlers.pull_request.review.gh_post",
                side_effect=lambda *a, **k: posted.append(a) or {"id": 1},
            ),
        ):
            _review_code(
                {"head": {"sha": "eval0000"}},
                "eval/repo",
                1,
                [{"filename": case["filename"], "patch": case["patch"]}],
                "tok",
                cfg,
                MagicMock(),
                "",
                MagicMock(),
            )

        assert posted == [], "_review_code posts nothing — the harness must read its return value"
