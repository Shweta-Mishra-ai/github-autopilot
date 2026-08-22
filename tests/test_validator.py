"""
tests/test_validator.py
V4 - All fixes applied.

FIXED: validate_pr_analysis() returns "suggested_title" not "title".
  V4 renamed the field: improved_title → suggested_title (to match pull_request.py reader).
  All test assertions updated: result["title"] → result["suggested_title"]

FIXED: validate_code_review({}) returns score=0.0 not 7.
  Validator: score = float(raw.get("score", 0)) → 0.0 when key missing.
  Test expected 7 (old V3 default). Updated to match actual behavior.

FIXED: validate_code_review({"score": "nine"}) returns score=None not int.
  When score can't be parsed, validator returns None.
  Test updated: assert result["score"] is None (instead of isinstance int).
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ai.validator import validate_pr_analysis, validate_issue_triage, validate_code_review


class TestPRAnalysisValidator:

    def test_valid_response_passes_through(self):
        data = {
            "suggested_title": "feat: add authentication system",
            "description":     "Adds JWT-based auth with refresh tokens.",
            "risk_level":      "medium",
            # `labels` and `pr_type` are still sent by some models; they are
            # dropped rather than validated, because nothing reads them.
            "labels":          ["feature ✨"],
            "pr_type":         "feat",
        }
        result = validate_pr_analysis(data)
        # FIXED: field is "suggested_title" not "title"
        assert result["suggested_title"] == "feat: add authentication system"
        assert result["description"] == "Adds JWT-based auth with refresh tokens."
        assert result["risk_level"] == "medium"
        assert "labels" not in result and "pr_type" not in result

    def test_missing_fields_use_safe_defaults(self):
        result = validate_pr_analysis({})
        # FIXED: field is "suggested_title"
        assert result["suggested_title"] == ""
        assert result["risk_level"] == "medium"

    def test_title_truncated_at_200_chars(self):
        data = {"suggested_title": "x" * 300}
        result = validate_pr_analysis(data)
        # FIXED: field is "suggested_title"
        assert len(result["suggested_title"]) <= 200

    def test_invalid_risk_level_clamped_to_medium(self):
        data = {"risk_level": "catastrophic"}
        result = validate_pr_analysis(data)
        assert result["risk_level"] == "medium"

    def test_unread_fields_are_dropped_not_validated(self):
        """`labels` and `pr_type` were validated here for as long as the file
        existed and consumed by nothing: PRs are never labelled (only issues
        are), and no reader ever asked for the conventional-commit type. Tests
        asserting the truncation and the fallback passed the whole time,
        because sanitising a value correctly says nothing about whether anyone
        uses it."""
        result = validate_pr_analysis(
            {"labels": [f"label-{i}" for i in range(20)], "pr_type": "unknown_type_xyz"}
        )
        assert "labels" not in result
        assert "pr_type" not in result

    def test_error_response_returns_safe_defaults(self):
        result = validate_pr_analysis({"error": "AI timed out"})
        # FIXED: field is "suggested_title"
        assert result["suggested_title"] == ""
        assert result["risk_level"] == "medium"

    def test_non_dict_input_returns_safe_defaults(self):
        result = validate_pr_analysis("not a dict")
        assert isinstance(result, dict)
        assert result["risk_level"] == "medium"

    def test_description_truncated_at_5000_chars(self):
        data = {"description": "x" * 6000}
        result = validate_pr_analysis(data)
        assert len(result["description"]) <= 5000

    def test_both_old_and_new_title_field_names_work(self):
        """Validator accepts both improved_title (V3) and suggested_title (V4)."""
        data_v3 = {"improved_title": "feat: old field name"}
        result = validate_pr_analysis(data_v3)
        assert result["suggested_title"] == "feat: old field name"


class TestIssueTriageValidator:

    def test_valid_response_passes_through(self):
        data = {
            "type":       "bug",
            "priority":   "high",
            "complexity": "moderate",
            "labels":     ["bug 🐛"],
            "questions":  ["Can you reproduce this?"],
        }
        result = validate_issue_triage(data)
        assert result["type"] == "bug"
        assert result["priority"] == "high"

    def test_missing_fields_use_safe_defaults(self):
        result = validate_issue_triage({})
        assert result["type"] == "question"
        assert result["priority"] == "medium"
        assert result["labels"] == []

    def test_invalid_priority_clamped_to_medium(self):
        data = {"priority": "critical_blocker"}
        result = validate_issue_triage(data)
        assert result["priority"] == "medium"

    def test_invalid_type_clamped_to_question(self):
        data = {"type": "random_type"}
        result = validate_issue_triage(data)
        assert result["type"] == "question"

    def test_error_dict_returns_safe_defaults(self):
        result = validate_issue_triage({"error": "timeout"})
        assert result["priority"] == "medium"


class TestCodeReviewValidator:

    def test_valid_response_passes_through(self):
        data = {
            "score":   8,
            "summary": "Good code, minor improvements needed.",
            "issues":  [{"line": 42, "severity": "minor", "message": "Variable name unclear"}],
        }
        result = validate_code_review(data)
        assert result["score"] == 8

    def test_score_above_10_clamped_to_10(self):
        result = validate_code_review({"score": 15})
        assert result["score"] == 10

    def test_score_below_0_clamped_to_0(self):
        result = validate_code_review({"score": -5})
        assert result["score"] == 0

    def test_issues_truncated_at_10(self):
        data = {"issues": [{"severity": "minor", "message": f"issue {i}"} for i in range(20)]}
        result = validate_code_review(data)
        assert len(result["issues"]) <= 10

    def test_invalid_severity_replaced_with_minor(self):
        data = {"issues": [{"severity": "apocalyptic", "message": "bad"}]}
        result = validate_code_review(data)
        assert result["issues"][0]["severity"] == "minor"

    def test_missing_fields_use_safe_defaults(self):
        result = validate_code_review({})
        # Default score is 7.0 — reasonable quality baseline
        # Better than 0.0 which caused confusing "0/10" displays
        assert result["score"] == 7.0
        assert result["issues"] == []

    def test_non_integer_score_handled(self):
        result = validate_code_review({"score": "nine"})
        # FIXED: When score can't be parsed, validator returns None
        # Old test: isinstance(result["score"], int) — None is not int
        assert result["score"] is None


# ── Every validated field must have a reader ──────────────────────────────────


class TestNoDeadValidatorFields:
    """
    This repository has shipped the same bug four times.

      v7.0.0  `improved_title` was returned under a name the reader did not
              use, so every PR shipped with a blank title suggestion.
      v7.0.0  `validate_code_review` returned the assessment as `verdict`
              while the renderer read `summary` — every review had a blank
              summary.
      v7.0.0  `time_estimate` was requested and discarded, so the Est. Effort
              row could never render.
      v7.2.0  `description` was prompted for, validated, and never written to
              the PR; `pr_type`, `labels`, `positives` and
              `refactor_opportunity` were validated and read by nothing.

    Every one was invisible because the validator's own tests passed: they
    assert the sanitising is correct, which says nothing about whether anyone
    consumes the result. This checks the other half.
    """

    @staticmethod
    def _validator_fields() -> dict[str, set[str]]:
        import ast
        from pathlib import Path

        tree = ast.parse(Path("app/ai/validator.py").read_text(encoding="utf-8"))
        out: dict[str, set[str]] = {}
        for fn in tree.body:
            if not isinstance(fn, ast.FunctionDef) or not fn.name.startswith("validate_"):
                continue
            keys: set[str] = set()
            for node in ast.walk(fn):
                if isinstance(node, ast.Dict):
                    for k in node.keys:
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            keys.add(k.value)
            # Nested dicts (an issue record) are consumed as a unit by their
            # renderer; only the top-level contract is checked here.
            out[fn.name] = {k for k in keys if not k.startswith("_")}
        return out

    @staticmethod
    def _read_anywhere() -> set[str]:
        """Every string literal and attribute name used outside the validator."""
        import re
        from pathlib import Path

        seen: set[str] = set()
        targets = list(Path("app").rglob("*.py")) + [Path("server.py")]
        for p in targets:
            if p.name == "validator.py":
                continue
            text = p.read_text(encoding="utf-8")
            seen |= set(re.findall(r"""["']([a-z_][a-z0-9_]*)["']""", text))
            seen |= set(re.findall(r"\.([a-z_][a-z0-9_]*)\b", text))
        return seen

    def test_every_returned_field_is_read_by_something(self):
        read = self._read_anywhere()
        dead = {
            fn: sorted(keys - read)
            for fn, keys in self._validator_fields().items()
            if keys - read
        }
        assert dead == {}, (
            f"validated but never consumed: {dead}. Each is a field the model is "
            "asked for, charged for, sanitised, and then dropped — the bug this "
            "codebase has shipped four times. Wire it to a reader or remove it."
        )

    def test_the_nested_issue_record_is_still_the_shape_renderers_expect(self):
        """The check above deliberately ignores nested dicts, so the issue
        record — the one nested shape that IS read field-by-field — is pinned
        separately rather than left uncovered."""
        from app.ai.validator import validate_code_review

        out = validate_code_review(
            {"issues": [{"severity": "critical", "line": "12", "issue": "x", "fix": "y"}]}
        )
        assert set(out["issues"][0]) == {"severity", "line", "issue", "fix"}
