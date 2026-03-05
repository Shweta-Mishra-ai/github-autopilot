"""
tests/test_validator.py
Pure unit tests for AI response validation.
No network calls needed.

Run: python -m pytest tests/test_validator.py -v
"""

import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ai.validator import validate_pr_analysis, validate_issue_triage, validate_code_review


class TestPRAnalysisValidator:

    def test_valid_response_passes_through(self):
        data = {
            "title": "feat: add authentication system",
            "description": "Adds JWT-based auth with refresh tokens.",
            "labels": ["feature ✨"],
            "risk_level": "medium",
            "pr_type": "feat"
        }
        result = validate_pr_analysis(data)
        assert result["title"] == "feat: add authentication system"
        assert result["risk_level"] == "medium"

    def test_missing_fields_use_safe_defaults(self):
        result = validate_pr_analysis({})
        assert result["title"] == ""
        assert result["risk_level"] == "medium"
        assert result["labels"] == []

    def test_title_truncated_at_200_chars(self):
        data = {"title": "x" * 300}
        result = validate_pr_analysis(data)
        assert len(result["title"]) <= 200

    def test_invalid_risk_level_clamped_to_medium(self):
        data = {"risk_level": "catastrophic"}
        result = validate_pr_analysis(data)
        assert result["risk_level"] == "medium"

    def test_labels_truncated_at_10(self):
        data = {"labels": [f"label-{i}" for i in range(20)]}
        result = validate_pr_analysis(data)
        assert len(result["labels"]) <= 10

    def test_invalid_pr_type_replaced_with_chore(self):
        data = {"pr_type": "unknown_type_xyz"}
        result = validate_pr_analysis(data)
        assert result["pr_type"] == "chore"

    def test_error_response_returns_safe_defaults(self):
        result = validate_pr_analysis({"error": "AI timed out"})
        assert result["title"] == ""
        assert result["risk_level"] == "medium"

    def test_non_dict_input_returns_safe_defaults(self):
        result = validate_pr_analysis("not a dict")
        assert isinstance(result, dict)
        assert result["risk_level"] == "medium"

    def test_description_truncated_at_5000_chars(self):
        data = {"description": "x" * 6000}
        result = validate_pr_analysis(data)
        assert len(result["description"]) <= 5000


class TestIssueTriageValidator:

    def test_valid_response_passes_through(self):
        data = {
            "type": "bug",
            "priority": "high",
            "complexity": "moderate",
            "labels": ["bug 🐛"],
            "questions": ["Can you reproduce this?"]
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
            "score": 8,
            "summary": "Good code, minor improvements needed.",
            "issues": [
                {"line": 42, "severity": "minor", "message": "Variable name unclear"}
            ]
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
        assert result["score"] == 7
        assert result["issues"] == []

    def test_non_integer_score_handled(self):
        result = validate_code_review({"score": "nine"})
        assert isinstance(result["score"], int)

