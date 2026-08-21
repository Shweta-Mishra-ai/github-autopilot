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


def check_pr_auto_merge(pr_data: dict, checks: list, reviews: list, config) -> GuardrailResult:
    if not config.auto_merge_enabled():
        return GuardrailResult(False, "Auto-merge disabled in .ai-repo-manager.yml")

    mergeable = pr_data.get("mergeable")
    if mergeable is False:
        return GuardrailResult(False, "PR has merge conflicts")
    if mergeable is None:
        return GuardrailResult(False, "GitHub hasn't computed mergeability yet — retry in a moment")

    if config.get("auto_merge", "require_no_blocking_reviews", default=True):
        blocking = [r for r in reviews if r.get("state") == "CHANGES_REQUESTED"]
        if blocking:
            # A change request from a since-deleted account has `user: null`.
            # Raising here turns "blocked by a review" into a generic failure,
            # which is the one message that does not tell the maintainer the
            # merge was correctly refused.
            blockers = ", ".join(
                f"@{(r.get('user') or {}).get('login', 'a deleted account')}" for r in blocking[:3]
            )
            return GuardrailResult(False, f"Blocked by change requests from: {blockers}")

    if config.get("auto_merge", "require_passing_checks", default=True):
        failed = [
            c
            for c in checks
            if c.get("conclusion") in ("failure", "cancelled", "timed_out", "action_required")
        ]
        if failed:
            names = ", ".join(c.get("name", "unnamed check") for c in failed[:3])
            return GuardrailResult(False, f"Failing checks: {names}")

    base = pr_data.get("base", {}).get("ref", "")
    protected = {"main", "master", "production", "release"}
    if base in protected and not config.get(
        "auto_merge", "allow_protected_branches", default=False
    ):
        return GuardrailResult(False, f"Target `{base}` is protected — auto-merge disabled")

    # allowed_risk_levels was documented and never consulted: a user who
    # restricted auto-merge to low-risk PRs still had high-risk ones merged.
    #
    # The risk level is produced by _analyze_pr on PR open and recorded via
    # record_pr_risk(). It is read back here rather than taken off pr_data,
    # because /merge fetches a raw GitHub PR object that carries no analysis.
    allowed_risks = config.get("auto_merge", "allowed_risk_levels", default=["low"])
    if isinstance(allowed_risks, list) and allowed_risks:
        risk = get_pr_risk(pr_data)
        allowed = {str(r).lower() for r in allowed_risks}
        if risk is None:
            # Fail closed: no analysis on record means we cannot show the PR
            # meets the operator's restriction. Say so rather than assume.
            return GuardrailResult(
                False,
                "No risk analysis on record for this PR, and "
                f"auto_merge.allowed_risk_levels restricts merging to "
                f"{', '.join(sorted(allowed))}. Re-open the PR to trigger "
                "analysis, or widen the setting.",
            )
        if risk not in allowed:
            return GuardrailResult(
                False,
                f"Risk level `{risk}` is not in auto_merge.allowed_risk_levels "
                f"({', '.join(sorted(allowed))})",
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
        return GuardrailResult(False, "Title already follows conventional commit format")
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
    except Exception as e:
        log.debug(f"guardrails.repo_rate_limit_check_failed repo={repo}: {e}")
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
    except Exception as e:
        log.debug(f"guardrails.increment_repo_usage_failed repo={repo}: {e}")


# ── PR risk record ────────────────────────────────────────────────────────────
#
# auto_merge.allowed_risk_levels needs the risk level that _analyze_pr computed
# on PR open, but /merge fetches a raw GitHub PR object which carries no
# analysis. Recording it keyed by head SHA means a force-push invalidates the
# record automatically: the risk of the old head says nothing about the new one.

_RISK_TTL = 7 * 86400


def _risk_key(repo: str, pr_number, head_sha: str) -> str:
    return f"pr_risk:{repo}:{pr_number}:{head_sha[:12]}"


def record_pr_risk(repo: str, pr_number, head_sha: str, risk: str) -> None:
    """Store the analysed risk level. Never raises — advisory data."""
    if not (repo and head_sha and risk):
        return
    try:
        from app.core.redis_client import get_redis

        get_redis().set(_risk_key(repo, pr_number, head_sha), str(risk).lower(), ex=_RISK_TTL)
    except Exception as e:
        log.debug(f"guardrails.record_pr_risk_failed repo={repo} pr={pr_number}: {e}")


def get_pr_risk(pr_data: dict) -> "str | None":
    """
    Recorded risk level for this PR's current head, or None when unknown.

    None is meaningful: the caller fails closed on it rather than assuming
    the PR is safe.
    """
    try:
        repo = (pr_data.get("base", {}).get("repo", {}) or {}).get("full_name", "")
        head_sha = (pr_data.get("head", {}) or {}).get("sha", "")
        number = pr_data.get("number")
        if not (repo and head_sha and number is not None):
            return None

        from app.core.redis_client import get_redis

        value = get_redis().get(_risk_key(repo, number, head_sha))
        return str(value).lower() if value else None
    except Exception as e:
        log.debug(f"guardrails.get_pr_risk_failed: {e}")
        return None
