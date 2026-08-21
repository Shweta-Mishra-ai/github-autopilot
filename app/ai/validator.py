"""
AI Response Validator - app/ai/validator.py
V4 changes:

FIXED (LOOPHOLE 18): Field name standardization.
  Old: validate_pr_analysis() returned {"improved_title": ...}
       But pull_request.py reads r.get("suggested_title") → always got None.
       PR title auto-update was silently using empty string.
  Fix: Return {"suggested_title": ...} everywhere to match the reader.

IMPROVED: Better defaults, stricter type checking, cleaner sanitization.
"""

import logging
from typing import Any

log = logging.getLogger(__name__)


def _get(data: dict, key: str, default: Any = None) -> Any:
    val = data.get(key, default)
    return val if val is not None else default


def _str(val: Any, max_len: int = 300) -> str:
    """Safe string conversion with length cap."""
    return str(val)[:max_len].strip() if val is not None else ""


def _list_of_str(val: Any, max_items: int = 10, max_item_len: int = 100) -> list[str]:
    """Safe list-of-strings extraction."""
    if not isinstance(val, list):
        return []
    return [str(item)[:max_item_len] for item in val if item][:max_items]


def is_unusable(raw: Any) -> bool:
    """
    True when an LLM payload must NOT be rendered as a real result.

    `_extract_json` returns {"raw": text} when the model produced no parseable
    JSON. That dict has no "error" key, so the old validators fell through to
    their defaults and published a fabricated result (e.g. "Score: 7/10 — no
    issues found") for a review that never happened. Treat it as a hard failure.
    """
    if not isinstance(raw, dict):
        return True
    return bool(raw.get("error")) or ("raw" in raw)


# ── PR Analysis ───────────────────────────────────────────────────────────────


def validate_pr_analysis(raw: dict) -> dict:
    """
    Validate and sanitize PR analysis response.

    Every field returned here is read by app/handlers/pull_request/analysis.py.
    That is a rule, not an observation: `pr_type` and `labels` used to be
    validated here and consumed nowhere, and `improved_title` was returned
    under a name the reader did not use, which is how every PR shipped with a
    blank title suggestion. tests/test_validator.py::TestNoDeadValidatorFields
    fails the build if a field is added here without a reader.
    """
    VALID_RISK = {"low", "medium", "high"}

    if is_unusable(raw):
        log.warning(f"validate_pr_analysis: unusable payload — {str(raw)[:120]}")
        return {
            "suggested_title": "",
            "description": "",
            "risk_level": "medium",
            "risk_reason": "Could not analyze — using safe defaults",
            "review_focus": [],
            "confidence": 0.0,
            "_degraded": True,
        }

    risk = _get(raw, "risk_level", "medium").lower()
    if risk not in VALID_RISK:
        risk = "medium"

    review_focus = raw.get("review_focus", [])
    if not isinstance(review_focus, list):
        review_focus = []
    review_focus = [str(f)[:200] for f in review_focus if f][:5]

    confidence = 0.5
    try:
        confidence = float(raw.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        pass

    return {
        "suggested_title": _str(raw.get("suggested_title") or raw.get("improved_title", ""), 200),
        "description": _str(raw.get("description", ""), 5000),
        "risk_level": risk,
        "risk_reason": _str(raw.get("risk_reason", ""), 300),
        "review_focus": review_focus,
        "confidence": confidence,
    }


# ── Issue Triage ──────────────────────────────────────────────────────────────


def validate_issue_triage(raw: dict) -> dict:
    """Validate and sanitize issue triage response."""
    # These MUST stay in sync with the enums the triage prompt asks for in
    # app/handlers/issues.py. When they drifted, "critical" fell out of the
    # allow-list and every critical issue was silently relabelled "medium" —
    # which is why security issue #76 carries `priority: medium`.
    VALID_TYPES = {"bug", "feature", "question", "docs", "performance", "security", "refactor"}
    VALID_PRIORITIES = {"critical", "high", "medium", "low"}
    VALID_COMPLEXITY = {"trivial", "simple", "moderate", "complex", "epic"}
    VALID_ESTIMATES = {"< 1 hour", "1-4 hours", "1-3 days", "1-2 weeks", "> 2 weeks"}

    if is_unusable(raw):
        log.warning(f"validate_issue_triage: unusable payload — {str(raw)[:120]}")
        return {
            "type": "question",
            "priority": "medium",
            "labels": [],
            "welcome": "Thanks for reporting this! We'll look into it.",
            "needs_info": False,
            "questions": [],
            "complexity": "moderate",
            "time_estimate": "",
            "_degraded": True,
        }

    issue_type = _get(raw, "type", "question").lower()
    if issue_type not in VALID_TYPES:
        issue_type = "question"

    priority = _get(raw, "priority", "medium").lower()
    if priority not in VALID_PRIORITIES:
        priority = "medium"

    complexity = _get(raw, "complexity", "moderate").lower()
    if complexity not in VALID_COMPLEXITY:
        complexity = "moderate"

    questions = raw.get("questions", [])
    if not isinstance(questions, list):
        questions = []
    questions = [str(q)[:200] for q in questions if q][:3]

    time_estimate = _str(raw.get("time_estimate", ""), 20)
    if time_estimate not in VALID_ESTIMATES:
        time_estimate = ""

    return {
        "type": issue_type,
        "priority": priority,
        "labels": _list_of_str(raw.get("labels"), max_items=8, max_item_len=50),
        "welcome": _str(raw.get("welcome", "Thanks for reporting this!"), 500),
        "needs_info": bool(raw.get("needs_info", False)),
        "questions": questions,
        "complexity": complexity,
        "time_estimate": time_estimate,
    }


# ── Code Review ───────────────────────────────────────────────────────────────


def validate_code_review(raw: dict) -> dict:
    """Validate code review for a single file."""
    if is_unusable(raw):
        log.warning(f"validate_code_review: unusable payload — {str(raw)[:120]}")
        return {
            "score": None,
            "summary": "",
            "issues": [],
            "confidence": 0.0,
            "_degraded": True,
        }

    # Score: float 0-10
    score = None
    try:
        score = float(raw.get("score", 7.0))  # default 7 = acceptable quality
        score = max(0.0, min(10.0, score))
    except (TypeError, ValueError):
        score = None

    # Issues: list of dicts with severity + issue + fix
    VALID_SEVERITIES = {"critical", "major", "minor", "nit"}
    raw_issues = raw.get("issues", [])
    if not isinstance(raw_issues, list):
        raw_issues = []

    clean_issues = []
    for item in raw_issues[:10]:
        if not isinstance(item, dict):
            continue
        sev = str(item.get("severity", "minor")).lower()
        if sev not in VALID_SEVERITIES:
            sev = "minor"
        clean_issues.append(
            {
                "severity": sev,
                "line": _str(item.get("line", ""), 20),
                "issue": _str(item.get("issue", ""), 300),
                "fix": _str(item.get("fix", ""), 500),
            }
        )

    confidence = 0.5
    try:
        confidence = float(raw.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        pass

    # The model's overall assessment. "verdict" is still accepted as an INPUT
    # alias because older prompts emitted that name, but it is not returned:
    # the duplicate output field was justified by a comment claiming
    # app/mcp/handlers.py and evals/ read it, and neither ever did.
    assessment = _str(raw.get("summary") or raw.get("verdict", ""), 200)

    return {
        "score": score,
        "summary": assessment,
        "issues": clean_issues,
        "confidence": confidence,
    }
