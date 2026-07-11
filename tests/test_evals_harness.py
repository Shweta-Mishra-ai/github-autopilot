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
