"""
tests/test_v7_correctness.py — V7 Phase 1.

These tests assert on RENDERED OUTPUT, not on validator return values. That is
the discipline the existing suite lacked: 908 tests passed while the bot was
publishing "Score: 7/10 — no issues found" for reviews that never ran.
"""

from unittest.mock import MagicMock, patch

from app.ai.validator import (
    is_unusable,
    validate_code_review,
    validate_issue_triage,
    validate_pr_analysis,
)


class TestUnusableGuard:
    def test_raw_key_is_unusable(self):
        assert is_unusable({"raw": "Sorry, I cannot help with that."}) is True

    def test_error_key_is_unusable(self):
        assert is_unusable({"error": "timeout"}) is True

    def test_non_dict_is_unusable(self):
        assert is_unusable("not a dict") is True

    def test_good_payload_is_usable(self):
        assert is_unusable({"score": 8, "issues": []}) is False

    def test_code_review_marks_raw_as_degraded(self):
        out = validate_code_review({"raw": "Sorry, I cannot help."})
        assert out["_degraded"] is True

    def test_code_review_does_not_invent_a_passing_score(self):
        """The 7.0 default must never reach a renderer for unparseable output."""
        out = validate_code_review({"raw": "Sorry, I cannot help."})
        assert out["score"] is None
        assert out["issues"] == []

    def test_triage_marks_raw_as_degraded(self):
        assert validate_issue_triage({"raw": "..."})["_degraded"] is True

    def test_pr_analysis_marks_raw_as_degraded(self):
        assert validate_pr_analysis({"raw": "..."})["_degraded"] is True

    def test_good_payload_is_not_degraded(self):
        out = validate_code_review({"score": 9, "issues": [], "summary": "fine"})
        assert out.get("_degraded", False) is False
