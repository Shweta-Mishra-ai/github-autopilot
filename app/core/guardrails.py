"""
Guardrails - app/core/guardrails.py

Deterministic safety checks that run BEFORE any automated action.
These are rule-based (no AI) — they must be fast, predictable, and never fail silently.

Rule: If a guardrail fails → action is SKIPPED, reason is logged.
      Bot may post an informational comment but never takes the risky action.
"""

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class GuardrailResult:
    passed: bool
    reason: str
    action_taken: str = ""   # what was attempted


def check_pr_auto_merge(pr_data: dict, checks: list, reviews: list, config) -> GuardrailResult:
    """
    ALL conditions must pass before auto-merging a PR.
    Even one failure → do not merge.
    """

    # 1. Config must explicitly enable auto-merge
    if not config.auto_merge_enabled():
        return GuardrailResult(
            passed=False,
            reason="Auto-merge is disabled in .ai-repo-manager.yml (set auto_merge.enabled: true to enable)"
        )

    # 2. PR must be mergeable
    mergeable = pr_data.get("mergeable")
    if mergeable is False:
        return GuardrailResult(
            passed=False,
            reason="PR has merge conflicts — cannot auto-merge"
        )

    if mergeable is None:
        # GitHub hasn't computed mergeability yet
        return GuardrailResult(
            passed=False,
            reason="GitHub hasn't finished computing mergeability — skipping auto-merge"
        )

    # 3. No blocking reviews
    if config.get("auto_merge", "require_no_blocking_reviews", default=True):
        blocking = [r for r in reviews if r.get("state") == "CHANGES_REQUESTED"]
        if blocking:
            blockers = ", ".join(f"@{r['user']['login']}" for r in blocking[:3])
            return GuardrailResult(
                passed=False,
                reason=f"Blocked by change requests from: {blockers}"
            )

    # 4. Required status checks must pass
    if config.get("auto_merge", "require_passing_checks", default=True):
        failed = [
            c for c in checks
            if c.get("conclusion") in ("failure", "cancelled", "timed_out", "action_required")
        ]
        if failed:
            names = ", ".join(c["name"] for c in failed[:3])
            return GuardrailResult(
                passed=False,
                reason=f"Failing checks: {names}"
            )

    # 5. PR must be to a non-protected branch (don't auto-merge to main without approval)
    base = pr_data.get("base", {}).get("ref", "")
    protected_targets = {"main", "master", "production", "release"}
    if base in protected_targets:
        # Allow only if explicitly configured
        if not config.get("auto_merge", "allow_protected_branches", default=False):
            return GuardrailResult(
                passed=False,
                reason=f"Target branch `{base}` is protected — auto-merge disabled for protected branches"
            )

    # 6. PR must not be a draft
    if pr_data.get("draft", False):
        return GuardrailResult(
            passed=False,
            reason="PR is a draft — will not auto-merge drafts"
        )

    # 7. PR must have at least 1 commit
    commits_count = pr_data.get("commits", 0)
    if commits_count == 0:
        return GuardrailResult(
            passed=False,
            reason="PR has no commits"
        )

    return GuardrailResult(passed=True, reason="All guardrails passed")


def check_auto_label(issue_or_pr: dict, labels: list, config) -> GuardrailResult:
    """Check before adding labels automatically."""

    if not config.get("issues", "auto_label", default=True):
        return GuardrailResult(passed=False, reason="Auto-label disabled in config")

    if not labels:
        return GuardrailResult(passed=False, reason="No labels to add")

    # Don't re-label already-labeled items
    existing = [l["name"] for l in issue_or_pr.get("labels", [])]
    new_labels = [l for l in labels if l not in existing]
    if not new_labels:
        return GuardrailResult(passed=False, reason="Labels already applied")

    return GuardrailResult(passed=True, reason="OK", action_taken=f"Adding: {new_labels}")


def check_title_update(current_title: str, new_title: str, config) -> GuardrailResult:
    """Check before auto-updating PR title."""

    if not config.get("pull_requests", "auto_polish_title", default=True):
        return GuardrailResult(passed=False, reason="Title auto-polish disabled in config")

    if not new_title or not new_title.strip():
        return GuardrailResult(passed=False, reason="AI returned empty title")

    if new_title == current_title:
        return GuardrailResult(passed=False, reason="Title unchanged — skipping update")

    # Don't update if title is already conventional commit format
    import re
    CONVENTIONAL = re.compile(
        r'^(feat|fix|docs|style|refactor|perf|test|chore|ci|build|revert)(\(.+\))?(!)?: .+',
        re.IGNORECASE
    )
    if CONVENTIONAL.match(current_title):
        return GuardrailResult(
            passed=False,
            reason="Title already follows conventional commit format — skipping"
        )

    return GuardrailResult(passed=True, reason="OK")


def check_description_update(current_body: str, config) -> GuardrailResult:
    """Check before auto-filling PR description."""

    if not config.get("pull_requests", "auto_fill_description", default=True):
        return GuardrailResult(passed=False, reason="Auto-fill description disabled in config")

    # Only fill if description is empty or minimal
    if current_body and len(current_body.strip()) >= 50:
        return GuardrailResult(
            passed=False,
            reason="PR already has a description — skipping auto-fill"
        )

    return GuardrailResult(passed=True, reason="OK")
