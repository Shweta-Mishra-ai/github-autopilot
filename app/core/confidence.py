"""
Confidence Gate - app/core/confidence.py
V3: Per-action confidence scoring.
Low confidence = suggest only, don't auto-apply.
"""

from app.core.logger import get_logger

log = get_logger(__name__)

# Default thresholds per action (0.0 - 1.0)
DEFAULT_THRESHOLDS = {
    "pr_title_rewrite": 0.85,
    "pr_description": 0.80,
    "issue_label": 0.75,
    "auto_merge": 0.95,
    "fix_command": 0.70,
    "secret_detection": 0.90,
    "code_review": 0.75,
    "issue_triage": 0.75,
}


class ConfidenceGate:
    def __init__(self, config=None):
        self._thresholds = DEFAULT_THRESHOLDS.copy()
        if config:
            overrides = config.get("confidence", "thresholds", default={})
            if isinstance(overrides, dict):
                self._thresholds.update(overrides)

    def should_auto_apply(self, action: str, score: float) -> bool:
        threshold = self._thresholds.get(action, 0.80)
        return score >= threshold

    def evaluate(self, action: str, ai_response: dict) -> dict:
        """
        Evaluate AI response confidence and decide auto-apply.
        Expects ai_response to contain a 'confidence' field (0.0-1.0).
        Falls back to 0.5 if not present.
        """
        score = float(ai_response.get("confidence", 0.5))
        auto_apply = self.should_auto_apply(action, score)

        log.info(
            "confidence.evaluated",
            action=action,
            score=score,
            auto_apply=auto_apply,
            threshold=self._thresholds.get(action, 0.80),
        )

        return {
            **ai_response,
            "confidence_score": score,
            "auto_apply": auto_apply,
            "confidence_note": (
                None
                if auto_apply
                else f"Confidence {score:.0%} below threshold — posted for human review."
            ),
        }
