"""
Confidence Gate - app/core/confidence.py
V3: Per-action confidence scoring.
Low confidence = suggest only, don't auto-apply.
"""

import contextlib

from app.core.logger import EventLogger

# EventLogger (not get_logger/stdlib Logger) because evaluate() below logs
# structured kwargs (action=, score=, ...) that a plain Logger rejects.
log = EventLogger("confidence")

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


# Weights for compute_confidence(). The model's own claim is deliberately the
# smallest term: it is uncalibrated, and a hallucinating model reports high
# confidence just as readily as a correct one. The other three are observable
# properties of the response rather than assertions made inside it.
_W_SELF_REPORTED = 0.15
_W_HALLUCINATION = 0.35
_W_ANCHOR_RATE = 0.25
_W_COMPLETENESS = 0.25

# A field shorter than this is treated as absent rather than answered.
_MIN_FIELD_CHARS = 10


def compute_confidence(
    payload,
    *,
    hallucination=None,
    anchor_rate: float | None = None,
    required_fields: tuple = (),
) -> float:
    """
    Confidence derived from evidence rather than from the model's own claim.

    Signals:
      self_reported  — what the model said (weak, kept for continuity)
      hallucination  — check_response() confidence
      anchor_rate    — fraction of findings that mapped to real diff lines
      completeness   — required fields present and non-trivial

    Terms with no signal available are dropped and the remaining weights
    renormalised, so a caller that cannot supply anchor_rate is not penalised
    for its absence.

    Returns 0.0 for a degraded or non-dict payload — those must never
    auto-apply anything.
    """
    if not isinstance(payload, dict) or payload.get("_degraded"):
        return 0.0

    terms: list[tuple[float, float]] = []

    try:
        self_reported = max(0.0, min(1.0, float(payload.get("confidence", 0.5))))
    except (TypeError, ValueError):
        self_reported = 0.5
    terms.append((_W_SELF_REPORTED, self_reported))

    if hallucination is not None:
        terms.append((_W_HALLUCINATION, float(getattr(hallucination, "confidence", 0.5))))

    if anchor_rate is not None:
        with contextlib.suppress(TypeError, ValueError):
            terms.append((_W_ANCHOR_RATE, max(0.0, min(1.0, float(anchor_rate)))))

    if required_fields:
        present = sum(
            1
            for f in required_fields
            if isinstance(payload.get(f), str)
            and len(payload.get(f, "").strip()) >= _MIN_FIELD_CHARS
        )
        terms.append((_W_COMPLETENESS, present / len(required_fields)))

    total_weight = sum(w for w, _ in terms)
    if not total_weight:
        return 0.5
    return round(sum(w * v for w, v in terms) / total_weight, 3)


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

    def evaluate(self, action: str, ai_response: dict, **signals) -> dict:
        """
        Evaluate AI response confidence and decide auto-apply.

        `signals` accepts hallucination=, anchor_rate= and required_fields=,
        forwarded to compute_confidence(). Callers that pass nothing still get
        a sane score; callers that pass signals get a calibrated one.

        This used to read ai_response["confidence"] directly — a number the
        model invents about itself. Every threshold in the config was being
        compared against an assertion rather than evidence.
        """
        score = compute_confidence(ai_response, **signals)
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
