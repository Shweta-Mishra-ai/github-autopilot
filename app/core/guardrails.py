"""
Guardrails - app/core/guardrails.py
V4: Deterministic safety checks before any automated action.

FIXED (BUG 1): check_title_update → check_pr_title_update
               check_description_update → check_pr_description_update
FIXED (ruff E741 lines 103,104): Renamed ambiguous `l` → `lbl`.
"""

import re
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

CONVENTIONAL = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|chore|ci|build|revert)(\(.+\))?(!)?: .+",
    re.IGNORECASE,
)


@dataclass
class GuardrailResult:
    passed: bool
    reason: str
    action_taken: str = ""


def check_pr_auto_merge(
    pr_data: dict, checks: list, reviews: list, config
) -> GuardrailResult:
    if not config.auto_merge_enabled():
        return GuardrailResult(False, "Auto-merge disabled in .ai-repo-manager.yml")

    mergeable = pr_data.get("mergeable")
    if mergeable is False:
        return GuardrailResult(False, "PR has merge conflicts")
    if mergeable is None:
        return GuardrailResult(
            False, "GitHub hasn't computed mergeability yet — retry in a moment"
        )

    if config.get("auto_merge", "require_no_blocking_reviews", default=True):
        blocking = [r for r in reviews if r.get("state") == "CHANGES_REQUESTED"]
        if blocking:
            blockers = ", ".join(f"@{r['user']['login']}" for r in blocking[:3])
            return GuardrailResult(
                False, f"Blocked by change requests from: {blockers}"
            )

    if config.get("auto_merge", "require_passing_checks", default=True):
        failed = [
            c
            for c in checks
            if c.get("conclusion")
            in ("failure", "cancelled", "timed_out", "action_required")
        ]
        if failed:
            names = ", ".join(c["name"] for c in failed[:3])
            return GuardrailResult(False, f"Failing checks: {names}")

    base = pr_data.get("base", {}).get("ref", "")
    protected = {"main", "master", "production", "release"}
    if base in protected and not config.get(
        "auto_merge", "allow_protected_branches", default=False
    ):
        return GuardrailResult(
            False, f"Target `{base}` is protected — auto-merge disabled"
        )

    if pr_data.get("draft", False):
        return GuardrailResult(False, "Draft PRs cannot be auto-merged")

    if pr_data.get("commits", 0) == 0:
        return GuardrailResult(False, "PR has no commits")

    return GuardrailResult(True, "All guardrails passed")


def check_auto_label(issue_or_pr: dict, labels: list, config) -> GuardrailResult:
    if not config.get("issues", "auto_label", default=True):
        return GuardrailResult(False, "Auto-label disabled in config")
    if not labels:
        return GuardrailResult(False, "No labels to add")

    # FIXED (E741): renamed `l` → `lbl`
    existing = [lbl["name"] for lbl in issue_or_pr.get("labels", [])]
    new_labels = [lbl for lbl in labels if lbl not in existing]
    if not new_labels:
        return GuardrailResult(False, "Labels already applied")

    return GuardrailResult(True, "OK", action_taken=f"Adding: {new_labels}")


def check_pr_title_update(pr: dict, config) -> GuardrailResult:
    if not config.get("pull_requests", "auto_polish_title", default=True):
        return GuardrailResult(False, "Title auto-polish disabled")
    current_title = pr.get("title", "")
    if not current_title:
        return GuardrailResult(False, "PR has no title")
    if CONVENTIONAL.match(current_title):
        return GuardrailResult(
            False, "Title already follows conventional commit format"
        )
    return GuardrailResult(True, "OK")


def check_pr_description_update(pr: dict, config) -> GuardrailResult:
    if not config.get("pull_requests", "auto_fill_description", default=True):
        return GuardrailResult(False, "Auto-fill description disabled")
    body = pr.get("body", "") or ""
    if len(body.strip()) >= 50:
        return GuardrailResult(False, "PR already has a description")
    return GuardrailResult(True, "OK")


def check_archived_repo(repo_data: dict) -> GuardrailResult:
    if repo_data.get("archived", False):
        return GuardrailResult(False, "Repository is archived — no actions taken")
    return GuardrailResult(True, "OK")


def check_repo_rate_limit(repo: str) -> GuardrailResult:
    try:
        from app.core.redis_client import get_redis
        import datetime
        import os

        limit = int(os.environ.get("REPO_DAILY_AI_LIMIT", "150"))
        today = datetime.date.today().isoformat()
        key = f"limit:{repo}:ai_calls:{today}"
        r = get_redis()
        count = int(r.get(key) or 0)

        if count >= limit:
            return GuardrailResult(
                False, f"Daily AI call limit ({limit}) reached. Resets at midnight UTC."
            )
    except Exception:
        pass
    return GuardrailResult(True, "OK")


def increment_repo_usage(repo: str):
    try:
        from app.core.redis_client import get_redis
        import datetime

        r = get_redis()
        today = datetime.date.today().isoformat()
        key = f"limit:{repo}:ai_calls:{today}"
        r.incr(key)
        r.expire(key, 86400)
    except Exception:
        pass
