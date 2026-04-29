"""
Tests - tests/test_confidence.py
V3: Unit tests for confidence scoring system.
"""

from app.core.confidence import ConfidenceGate, DEFAULT_THRESHOLDS


class TestConfidenceGate:

    def setup_method(self):
        self.gate = ConfidenceGate()

    def test_high_confidence_auto_applies(self):
        result = self.gate.evaluate("pr_title_rewrite", {"confidence": 0.95})
        assert result["auto_apply"] is True

    def test_low_confidence_blocks_auto_apply(self):
        result = self.gate.evaluate("pr_title_rewrite", {"confidence": 0.50})
        assert result["auto_apply"] is False

    def test_confidence_note_present_when_blocked(self):
        result = self.gate.evaluate("pr_title_rewrite", {"confidence": 0.50})
        assert result["confidence_note"] is not None
        assert "human review" in result["confidence_note"]

    def test_no_confidence_note_when_auto_applied(self):
        result = self.gate.evaluate("fix_command", {"confidence": 0.90})
        assert result["confidence_note"] is None

    def test_auto_merge_requires_very_high_confidence(self):
        result_low = self.gate.evaluate("auto_merge", {"confidence": 0.90})
        result_high = self.gate.evaluate("auto_merge", {"confidence": 0.96})
        assert result_low["auto_apply"] is False
        assert result_high["auto_apply"] is True

    def test_missing_confidence_defaults_to_half(self):
        result = self.gate.evaluate("fix_command", {})
        assert result["confidence_score"] == 0.5

    def test_confidence_score_preserved_in_result(self):
        result = self.gate.evaluate("code_review", {"confidence": 0.82})
        assert result["confidence_score"] == 0.82

    def test_original_response_fields_preserved(self):
        response = {"confidence": 0.90, "fix": "do this", "root_cause": "that"}
        result = self.gate.evaluate("fix_command", response)
        assert result["fix"] == "do this"
        assert result["root_cause"] == "that"

    def test_custom_threshold_via_config(self):
        class FakeConfig:
            def get(self, *keys, default=None):
                if keys == ("confidence", "thresholds"):
                    return {"fix_command": 0.99}
                return default

        gate = ConfidenceGate(FakeConfig())
        result = gate.evaluate("fix_command", {"confidence": 0.90})
        assert result["auto_apply"] is False

    def test_unknown_action_uses_default_threshold(self):
        result_low = self.gate.evaluate("unknown_action", {"confidence": 0.50})
        result_high = self.gate.evaluate("unknown_action", {"confidence": 0.85})
        assert result_low["auto_apply"] is False
        assert result_high["auto_apply"] is True

    def test_all_default_thresholds_are_valid(self):
        for action, threshold in DEFAULT_THRESHOLDS.items():
            assert 0.0 <= threshold <= 1.0, f"{action} threshold out of range"

