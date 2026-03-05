"""
AI Response Validator - app/ai/validator.py
Validates AI JSON responses before any action is taken.
If validation fails — safe defaults are returned, never crashes.
"""

import logging
from typing import Any

log = logging.getLogger(__name__)


def _get(data: dict, key: str, default: Any = None) -> Any:
    val = data.get(key, default)
    return val if val is not None else default


def validate_pr_analysis(raw: dict) -> dict:
    """Validate and sanitize PR analysis response."""
    if not isinstance(raw, dict) or raw.get("error"):
        log.warning(f"Invalid PR analysis response: {raw}")
        return {
            "improved_title": "",
            "description": "",
            "labels": [],
            "risk_level": "medium",
            "risk_reason": "Could not analyze",
            "reviewer_focus": "General review",
            "pr_type": "chore",
        }

    VALID_RISK = {"low", "medium", "high"}
    VALID_TYPES = {"feat", "fix", "docs", "refactor", "test", "chore", "perf", "ci", "style"}

    risk = raw.get("risk_level", "medium").lower()
    if risk not in VALID_RISK:
        risk = "medium"

    pr_type = raw.get("pr_type", "chore").lower()
    if pr_type not in VALID_TYPES:
        pr_type = "chore"

    # Sanitize labels — must be list of strings, max 10
    labels = raw.get("labels", [])
    if not isinstance(labels, list):
        labels = []
    labels = [str(l)[:50] for l in labels if l][:10]

    return {
        "improved_title": str(raw.get("improved_title", ""))[:200].strip(),
        "description": str(raw.get("description", ""))[:5000].strip(),
        "labels": labels,
        "risk_level": risk,
        "risk_reason": str(raw.get("risk_reason", ""))[:300].strip(),
        "reviewer_focus": str(raw.get("reviewer_focus", "General review"))[:300].strip(),
        "pr_type": pr_type,
    }


def validate_issue_triage(raw: dict) -> dict:
    """Validate and sanitize issue triage response."""
    if not isinstance(raw, dict) or raw.get("error"):
        return {
            "type": "question",
            "priority": "medium",
            "labels": [],
            "welcome": "Thanks for reporting this!",
            "needs_info": False,
            "questions": [],
            "complexity": "moderate",
        }

    VALID_TYPES = {"bug", "feature", "question", "docs", "performance", "security"}
    VALID_PRIORITIES = {"high", "medium", "low"}
    VALID_COMPLEXITY = {"trivial", "simple", "moderate", "complex"}

    issue_type = raw.get("type", "question").lower()
    if issue_type not in VALID_TYPES:
        issue_type = "question"

    priority = raw.get("priority", "medium").lower()
    if priority not in VALID_PRIORITIES:
        priority = "medium"

    complexity = raw.get("complexity", "moderate").lower()
    if complexity not in VALID_COMPLEXITY:
        complexity = "moderate"

    labels = raw.get("labels", [])
    if not isinstance(labels, list):
        labels = []
    labels = [str(l)[:50] for l in labels if l][:8]

    questions = raw.get("questions", [])
    if not isinstance(questions, list):
        questions = []
    questions = [str(q)[:200] for q in questions if q][:3]

    return {
        "type": issue_type,
        "priority": priority,
        "labels": labels,
        "welcome": str(raw.get("welcome", "Thanks for reporting this!"))[:500].strip(),
        "needs_info": bool(raw.get("needs_info", False)),
        "questions": questions,
        "complexity": complexity,
    }


def validate_code_review(raw: dict) -> dict:
    """Validate code review for a single file."""
    if not isinstance(raw, dict) or raw.get("error"):
        return {"score": None, "verdict": "", "issues": [], "positives": []}

    score = raw.get("score")
    try:
        score = float(score)
        # Clamp to 0-10
        score = max(0.0, min(10.0, score))
    except (TypeError, ValueError):
        score = None

    issues = raw.get("issues", [])
    if not isinstance(issues, list):
        issues = []

    VALID_SEVERITIES = {"critical", "major", "minor", "nit"}
    clean_issues = []
    for issue in issues[:10]:
        if not isinstance(issue, dict):
            continue
        sev = issue.get("severity", "minor").lower()
        if sev not in VALID_SEVERITIES:
            sev = "minor"
        clean_issues.append({
            "severity": sev,
            "issue": str(issue.get("issue", ""))[:300],
            "fix": str(issue.get("fix", ""))[:500],
        })

    positives = raw.get("positives", [])
    if not isinstance(positives, list):
        positives = []
    positives = [str(p)[:200] for p in positives if p][:5]

    return {
        "score": score,
        "verdict": str(raw.get("verdict", ""))[:200].strip(),
        "issues": clean_issues,
        "positives": positives,
        "refactor_opportunity": str(raw.get("refactor_opportunity", ""))[:300].strip(),
    }
