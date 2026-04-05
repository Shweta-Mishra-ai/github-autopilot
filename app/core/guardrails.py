"""
Guardrails - app/core/guardrails.py
V4: Deterministic safety checks before any automated action.

FIXED (BUG 1): Function names renamed to match pull_request.py imports:
  check_title_update       → check_pr_title_update
  check_description_update → check_pr_description_update

V4 NEW:
  + check_archived_repo()   — skip actions on archived repos
  + check_repo_rate_limit() — per-repo daily AI call cap
  + increment_repo_usage()  — tracks daily usage in Redis
"""

import re
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

CONVENTIONAL = re.compile(
    r'^(feat|fix|docs|style|refactor|perf|test|chore|ci|build|revert)(\(.+\))?(!)?: .+',
    re.IGNORECASE
)


@dataclass
class GuardrailResult:
    passed: bool
    reason: str
    action_taken: str = ""


# ── Existing guardrails (kept, BUG 1 fixed) ──────────────────────────────────

def check_pr_auto_merge(
    pr_data: dict, checks: list, reviews: list, config
) -> GuardrailResult:
    """All conditions must pass before auto-merging a PR."""

    if not config.auto_merge_enabled():
        return GuardrailResult(
            False,
            "Auto-merge is disabled in .ai-repo-manager.yml"
            " (set auto_merge.enabled: true to enable)"
        )

    mergeable = pr_data.get("mergeable")
    if mergeable is False:
        return GuardrailResult(False, "PR has merge conflicts — cannot auto-merge")
    if mergeable is None:
        return GuardrailResult(
            False,
            "GitHub hasn't finished computing mergeability — retry in a moment"
        )

    if config.get("auto_merge", "require_no_blocking_reviews", default=True):
        blocking = [r for r in reviews if r.get("state") == "CHANGES_REQUESTED"]
        if blocking:
            blockers = ", ".join(f"@{r['user']['login']}" for r in blocking[:3])
            return GuardrailResult(False, f"Blocked by change requests from: {blockers}")

    if config.get("auto_merge", "require_passing_checks", default=True):
        failed = [
            c for c in checks
            if c.get("conclusion") in
            ("failure", "cancelled", "timed_out", "action_required")
        ]
        if failed:
            names = ", ".join(c["name"] for c in failed[:3])
            return GuardrailResult(False, f"Failing checks: {names}")

    base = pr_data.get("base", {}).get("ref", "")
    protected = {"main", "master", "production", "release"}
    if base in protected:
        if not config.get("auto_merge", "allow_protected_branches", default=False):
            return GuardrailResult(
                False,
                f"Target branch `{base}` is protected"
                " — auto-merge disabled for protected branches"
            )

    if pr_data.get("draft", False):
        return GuardrailResult(False, "PR is a draft — will not auto-merge drafts")

    if pr_data.get("commits", 0) == 0:
        return GuardrailResult(False, "PR has no commits")

    return GuardrailResult(True, "All guardrails passed")


def check_auto_label(
    issue_or_pr: dict, labels: list, config
) -> GuardrailResult:
    """Check before adding labels automatically."""

    if not config.get("issues", "auto_label", default=True):
        return GuardrailResult(False, "Auto-label disabled in config")

    if not labels:
        return GuardrailResult(False, "No labels to add")

    existing = [l["name"] for l in issue_or_pr.get("labels", [])]
    new_labels = [l for l in labels if l not in existing]
    if not new_labels:
        return GuardrailResult(False, "Labels already applied")

    return GuardrailResult(True, "OK", action_taken=f"Adding: {new_labels}")


def check_pr_title_update(pr: dict, config) -> GuardrailResult:
    """
    Check before auto-updating PR title.
    ✅ FIXED (BUG 1): Was check_title_update() — pull_request.py couldn't import it.
    """
    if not config.get("pull_requests", "auto_polish_title", default=True):
        return GuardrailResult(False, "Title auto-polish disabled in config")

    current_title = pr.get("title", "")
    if not current_title:
        return GuardrailResult(False, "PR has no title")

    if CONVENTIONAL.match(current_title):
        return GuardrailResult(
            False,
            "Title already follows conventional commit format — skipping"
        )

    return GuardrailResult(True, "OK")


def check_pr_description_update(pr: dict, config) -> GuardrailResult:
    """
    Check before auto-filling PR description.
    ✅ FIXED (BUG 1): Was check_description_update() — pull_request.py couldn't import it.
    """
    if not config.get("pull_requests", "auto_fill_description", default=True):
        return GuardrailResult(False, "Auto-fill description disabled in config")

    body = pr.get("body", "") or ""
    if len(body.strip()) >= 50:
        return GuardrailResult(
            False,
            "PR already has a description — skipping auto-fill"
        )

    return GuardrailResult(True, "OK")


# ── V4 NEW guardrails ────────────────────────────────────────────────────────

def check_archived_repo(repo_data: dict) -> GuardrailResult:
    """
    V4 NEW (LOOPHOLE 15): Skip ALL actions on archived repos.
    Archived repos reject comment POST with 403 anyway.
    """
    if repo_data.get("archived", False):
        return GuardrailResult(
            False,
            "Repository is archived — no actions taken on archived repos"
        )
    return GuardrailResult(True, "OK")


def check_repo_rate_limit(repo: str) -> GuardrailResult:
    """
    V4 NEW: Per-repo daily AI call cap.
    Prevents one heavy repo starving others on free tier.
    Default: 150 AI calls/day per repo (configurable via REPO_DAILY_AI_LIMIT env var).
    """
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
                False,
                f"Daily AI call limit ({limit}) reached for this repo. "
                "Resets at midnight UTC."
            )
    except Exception:
        pass  # If Redis unavailable, don't block the action

    return GuardrailResult(True, "OK")


def increment_repo_usage(repo: str):
    """
    V4 NEW: Increment per-repo daily AI call counter after each AI call.
    Call this AFTER a successful AI action, not before.
    """
    try:
        from app.core.redis_client import get_redis
        import datetime

        r = get_redis()
        today = datetime.date.today().isoformat()
        key = f"limit:{repo}:ai_calls:{today}"
        r.incr(key)
        r.expire(key, 86400)  # Auto-expire after 24h
    except Exception:
        pass
