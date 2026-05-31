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


# ── PR Analysis ───────────────────────────────────────────────────────────────


def validate_pr_analysis(raw: dict) -> dict:
    """
    Validate and sanitize PR analysis response.

    ✅ FIXED (LOOPHOLE 18): Returns "suggested_title" (was "improved_title").
    pull_request.py reads r.get("suggested_title") — field name now matches.
    """
    VALID_RISK = {"low", "medium", "high"}
    VALID_TYPES = {
        "feat",
        "fix",
        "docs",
        "refactor",
        "test",
        "chore",
        "perf",
        "ci",
        "style",
        "build",
    }

    if not isinstance(raw, dict) or raw.get("error"):
        log.warning(f"validate_pr_analysis: invalid response — {raw}")
        return {
            "suggested_title": "",  # ✅ FIXED field name
            "description": "",
            "labels": [],
            "risk_level": "medium",
            "risk_reason": "Could not analyze — using safe defaults",
            "review_focus": [],
            "pr_type": "chore",
            "confidence": 0.5,
        }

    risk = _get(raw, "risk_level", "medium").lower()
    if risk not in VALID_RISK:
        risk = "medium"

    pr_type = _get(raw, "pr_type", "chore").lower()
    if pr_type not in VALID_TYPES:
        pr_type = "chore"

    labels = _list_of_str(raw.get("labels"), max_items=10, max_item_len=50)

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
        "suggested_title": _str(
            raw.get("suggested_title") or raw.get("improved_title", ""), 200
        ),
        "description": _str(raw.get("description", ""), 5000),
        "labels": labels,
        "risk_level": risk,
        "risk_reason": _str(raw.get("risk_reason", ""), 300),
        "review_focus": review_focus,
        "pr_type": pr_type,
        "confidence": confidence,
    }


# ── Issue Triage ──────────────────────────────────────────────────────────────


def validate_issue_triage(raw: dict) -> dict:
    """Validate and sanitize issue triage response."""
    VALID_TYPES = {"bug", "feature", "question", "docs", "performance", "security"}
    VALID_PRIORITIES = {"high", "medium", "low"}
    VALID_COMPLEXITY = {"trivial", "simple", "moderate", "complex"}

    if not isinstance(raw, dict) or raw.get("error"):
        return {
            "type": "question",
            "priority": "medium",
            "labels": [],
            "welcome": "Thanks for reporting this! We'll look into it.",
            "needs_info": False,
            "questions": [],
            "complexity": "moderate",
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

    return {
        "type": issue_type,
        "priority": priority,
        "labels": _list_of_str(raw.get("labels"), max_items=8, max_item_len=50),
        "welcome": _str(raw.get("welcome", "Thanks for reporting this!"), 500),
        "needs_info": bool(raw.get("needs_info", False)),
        "questions": questions,
        "complexity": complexity,
    }


# ── Code Review ───────────────────────────────────────────────────────────────


def validate_code_review(raw: dict) -> dict:
    """Validate code review for a single file."""
    if not isinstance(raw, dict) or raw.get("error"):
        return {"score": None, "verdict": "", "issues": [], "positives": []}

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

    return {
        "score": score,
        "verdict": _str(raw.get("verdict") or raw.get("summary", ""), 200),
        "issues": clean_issues,
        "positives": _list_of_str(raw.get("positives"), max_items=5, max_item_len=200),
        "confidence": confidence,
        "refactor_opportunity": _str(raw.get("refactor_opportunity", ""), 300),
    }
