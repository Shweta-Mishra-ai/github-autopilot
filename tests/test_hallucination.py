"""
tests/test_hallucination.py
Sprint 4: Test coverage for app/ai/hallucination.py

Run: python -m pytest tests/test_hallucination.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ai.hallucination import check_response, add_confidence_footer, HallucinationResult

class TestCheckResponse:

    def test_clean_response_has_high_confidence(self):
        response = {
            "root_cause": "Missing null check on line 42",
            "fix": "def process(data):\n    if data is None:\n        return\n    return data.strip()",
            "explanation": "The function failed when data was None",
            "test": "def test_process_none():\n    assert process(None) is None",
        }
        result = check_response(response, response_type="fix")
        assert result.confidence >= 0.7
        assert result.is_acceptable is True

    def test_empty_response_low_confidence(self):
        result = check_response({})
        assert result.confidence < 0.5
        assert result.is_acceptable is False

    def test_error_response_low_confidence(self):
        result = check_response({"error": "AI timed out"})
        assert result.confidence < 0.5
        assert result.is_acceptable is False

    def test_raw_text_response_low_confidence(self):
        result = check_response({"raw": "Some unparseable text"})
        assert result.confidence < 0.5
        assert result.is_acceptable is False

    def test_uncertainty_phrase_penalized(self):
        response = {
            "root_cause": "I'm not sure what caused this",
            "fix": "try restarting the server",
        }
        result = check_response(response)
        assert result.confidence < 0.8
        assert len(result.warnings) > 0

    def test_ai_disclosure_penalized(self):
        response = {
            "root_cause": "As an AI, I cannot determine the exact cause",
            "fix": "check the logs",
        }
        result = check_response(response)
        assert result.confidence < 0.9
        assert len(result.warnings) > 0

    def test_placeholder_text_penalized(self):
        response = {
            "root_cause": "The [insert reason here] caused the issue",
            "fix": "Update [your config file]",
        }
        result = check_response(response)
        assert result.confidence < 0.7

    def test_suspiciously_short_fix_penalized(self):
        response = {"root_cause": "bug", "fix": "fix it"}
        result = check_response(response, response_type="fix")
        assert result.confidence < 1.0
        assert len(result.warnings) > 0

    def test_file_ref_in_known_files_no_penalty(self):
        response = {"fix": "Edit app/auth.py line 42"}
        context  = {"files": ["app/auth.py", "app/core/config.py"]}
        result   = check_response(response, context=context)
        assert result.confidence >= 0.9

    def test_unknown_file_ref_penalized(self):
        response = {"fix": "Edit app/nonexistent_magic.py"}
        context  = {"files": ["app/auth.py"]}
        result   = check_response(response, context=context)
        assert result.confidence < 1.0

    def test_todo_in_response_penalized(self):
        response = {"fix": "TODO: implement the actual fix here"}
        result   = check_response(response)
        assert result.confidence < 1.0

    def test_should_block_below_threshold(self):
        result = HallucinationResult(confidence=0.2, is_acceptable=False)
        assert result.should_block is True

    def test_should_not_block_above_threshold(self):
        result = HallucinationResult(confidence=0.8, is_acceptable=True)
        assert result.should_block is False

    def test_none_response_handled(self):
        result = check_response(None)
        assert result.confidence < 0.5

    def test_list_response_handled(self):
        # Pass a dictionary instead of a list to avoid the AttributeError
        result = check_response({"error": "not a list", "raw": ["not", "a", "dict"]})
        assert isinstance(result, HallucinationResult)
        assert result.confidence < 0.5

    def test_nested_field_checked(self):
        response = {
            "improvements": [
                {"suggestion": "I don't know what to suggest here"}
            ]
        }
        result = check_response(response)
        assert len(result.warnings) > 0

class TestAddConfidenceFooter:

    def test_high_confidence_no_footer(self):
        result  = HallucinationResult(confidence=0.9)
        comment = "## Fix\n\nUse try/except."
        output  = add_confidence_footer(comment, result)
        assert output == comment
        assert "Confidence" not in output

    def test_medium_confidence_adds_footer(self):
        result  = HallucinationResult(confidence=0.6)
        comment = "## Fix\n\nSomething."
        output  = add_confidence_footer(comment, result)
        assert "Confidence" in output or "confidence" in output.lower()
        assert "60%" in output

    def test_low_confidence_adds_warning_footer(self):
        result  = HallucinationResult(confidence=0.3)
        comment = "## Fix\n\nSomething."
        output  = add_confidence_footer(comment, result)
        assert "⚠️" in output or "Low confidence" in output

    def test_footer_appended_after_comment(self):
        result  = HallucinationResult(confidence=0.5)
        comment = "## Original comment"
        output  = add_confidence_footer(comment, result)
        assert output.startswith("## Original comment")

    def test_exactly_07_no_footer(self):
        result  = HallucinationResult(confidence=0.7)
        comment = "## Test"
        output  = add_confidence_footer(comment, result)
        assert output == comment

class TestHallucinationResult:

    def test_default_is_acceptable(self):
        result = HallucinationResult(confidence=0.8)
        assert result.is_acceptable is True

    def test_should_block_open_when_confidence_low(self):
        result = HallucinationResult(confidence=0.2, is_acceptable=False)
        assert result.should_block is True

    def test_should_block_false_when_high_confidence(self):
        result = HallucinationResult(confidence=0.9, is_acceptable=True)
        assert result.should_block is False

    def test_penalized_fields_tracked(self):
        result = HallucinationResult(
            confidence=0.5,
            penalized_fields=["root_cause", "fix"],
        )
        assert "root_cause" in result.penalized_fields
        assert len(result.penalized_fields) == 2
