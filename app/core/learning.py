"""
app/core/learning.py
V4 Sprint 7: Learning from user feedback.

Tracks which bot suggestions users accept vs ignore.
Future suggestions use this history to improve quality.
"""
import json
import logging
from datetime import datetime, timezone

from app.core.redis_client import get_redis

log = logging.getLogger(__name__)
LEARNING_TTL = 90 * 86400  # 90 days


def record_fix_accepted(repo: str, issue_number: int, fix_type: str):
    """User merged a bot-suggested fix or applied it."""
    _record_event(repo, "fix_accepted", {"issue": issue_number, "type": fix_type})
    _incr(f"learn:{repo}:fix_accepted:{fix_type}")


def record_fix_ignored(repo: str, issue_number: int, fix_type: str):
    """Issue closed without applying bot fix — likely ignored."""
    _record_event(repo, "fix_ignored", {"issue": issue_number, "type": fix_type})
    _incr(f"learn:{repo}:fix_ignored:{fix_type}")


def record_autofix_merged(repo: str, pr_number: int, issue_number: int):
    """Bot-created autofix PR was merged."""
    _record_event(repo, "autofix_merged", {"pr": pr_number, "issue": issue_number})
    _incr(f"learn:{repo}:autofix_merged")


def record_autofix_closed(repo: str, pr_number: int):
    """Bot-created autofix PR was closed without merging."""
    _record_event(repo, "autofix_closed", {"pr": pr_number})
    _incr(f"learn:{repo}:autofix_closed")


def get_acceptance_rate(repo: str, fix_type: str = "all") -> float:
    """Returns fix acceptance rate 0.0-1.0 for this repo."""
    try:
        r = get_redis()
        if fix_type == "all":
            accepted = sum(int(r.get(f"learn:{repo}:fix_accepted:{t}") or 0)
                          for t in ["code", "deps", "config", "docs"])
            ignored  = sum(int(r.get(f"learn:{repo}:fix_ignored:{t}") or 0)
                          for t in ["code", "deps", "config", "docs"])
        else:
            accepted = int(r.get(f"learn:{repo}:fix_accepted:{fix_type}") or 0)
            ignored  = int(r.get(f"learn:{repo}:fix_ignored:{fix_type}") or 0)
        total = accepted + ignored
        return round(accepted / total, 2) if total >= 5 else 0.5
    except Exception:
        return 0.5


def get_repo_patterns(repo: str) -> dict:
    """
    Returns learned patterns for this repo.
    Used by prompt_builder.py to customize AI prompts.
    """
    try:
        r = get_redis()
        raw = r.get(f"learn:{repo}:patterns")
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def update_repo_patterns(repo: str, patterns: dict):
    """Store repo-specific patterns learned from interactions."""
    try:
        r = get_redis()
        existing = get_repo_patterns(repo)
        existing.update(patterns)
        r.set(f"learn:{repo}:patterns", json.dumps(existing), ex=LEARNING_TTL)
    except Exception:
        pass


def get_learning_summary(repo: str) -> dict:
    """Returns learning stats for /health and /report commands."""
    try:
        r = get_redis()
        return {
            "fix_acceptance_rate": get_acceptance_rate(repo),
            "autofix_merged":   int(r.get(f"learn:{repo}:autofix_merged") or 0),
            "autofix_closed":   int(r.get(f"learn:{repo}:autofix_closed") or 0),
            "patterns_learned": len(get_repo_patterns(repo)),
        }
    except Exception:
        return {"fix_acceptance_rate": 0.5, "autofix_merged": 0,
                "autofix_closed": 0, "patterns_learned": 0}


def _record_event(repo: str, event_type: str, data: dict):
    try:
        r = get_redis()
        key = f"learn:{repo}:events"
        entry = {"type": event_type, "data": data,
                 "ts": datetime.now(timezone.utc).isoformat()}
        r.lpush(key, json.dumps(entry))
        r.ltrim(key, 0, 199)
        r.expire(key, LEARNING_TTL)
    except Exception:
        pass


def _incr(key: str):
    try:
        r = get_redis()
        r.incr(key)
        r.expire(key, LEARNING_TTL)
    except Exception:
        pass


# ==== Helper functions expected by tests ====

def record_fix_outcome(repo: str, command: str, accepted: bool):
    """Record that a user accepted or rejected a bot suggestion."""
    if accepted:
        record_fix_accepted(repo, 0, command)
    else:
        record_fix_ignored(repo, 0, command)


def record_pattern(repo: str, pattern: str, value: bool):
    """Record a learned pattern for a repo."""
    patterns = get_repo_patterns(repo)
    patterns[pattern] = value
    update_repo_patterns(repo, patterns)


def get_pattern_summary(repo: str) -> str:
    """Get summary of accepted patterns for prompt injection."""
    patterns = get_repo_patterns(repo)
    if not patterns:
        return ""
    return " " + ", ".join(f"{k}={v}" for k, v in patterns.items())
