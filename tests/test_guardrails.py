"""
tests/test_guardrails.py
Pure unit tests for guardrail logic.
No network calls, no GitHub API, no Groq API needed.

Run: python -m pytest tests/test_guardrails.py -v
"""

import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.guardrails import (
    check_pr_auto_merge,
    check_auto_label,
    check_title_update,
    check_description_update,
)


# ── Mock Config ──────────────────────────────────────────────────────────────

class MockConfig:
    """Minimal config mock for testing."""

    def __init__(self, auto_merge=True, polish_title=True,
                 fill_desc=True, auto_label=True,
                 require_checks=True, require_reviews=True,
                 allow_protected=False, risk_levels=None):
        self._auto_merge = auto_merge
        self._polish_title = polish_title
        self._fill_desc = fill_desc
        self._auto_label = auto_label
        self._require_checks = require_checks
        self._require_reviews = require_reviews
        self._allow_protected = allow_protected
        self._risk_levels = risk_levels or ["low"]

    def auto_merge_enabled(self): return self._auto_merge
    def auto_merge_risk_ok(self, risk): return risk in self._risk_levels

    def get(self, *keys, default=None):
        mapping = {
            ("auto_merge", "require_passing_checks"): self._require_checks,
            ("auto_merge", "require_no_blocking_reviews"): self._require_reviews,
            ("auto_merge", "allow_protected_branches"): self._allow_protected,
            ("pull_requests", "auto_polish_title"): self._polish_title,
            ("pull_requests", "auto_fill_description"): self._fill_desc,
            ("issues", "auto_label"): self._auto_label,
        }
        return mapping.get(keys, default)


# ── Fixtures ─────────────────────────────────────────────────────────────────

def make_pr(mergeable=True, mergeable_state="clean", draft=False,
            base_ref="feature/test", commits=3):
    return {
        "mergeable": mergeable,
        "mergeable_state": mergeable_state,
        "draft": draft,
        "commits": commits,
        "base": {"ref": base_ref},
        "labels": [],
    }


def make_checks(passing=True):
    if passing:
        return [{"name": "CI", "conclusion": "success"}]
    return [{"name": "CI", "conclusion": "failure"}]


def make_reviews(blocking=False):
    if blocking:
        return [{"state": "CHANGES_REQUESTED", "user": {"login": "reviewer1"}}]
    return [{"state": "APPROVED", "user": {"login": "reviewer1"}}]


# ── Tests: check_pr_auto_merge ────────────────────────────────────────────────

class TestAutoMergeGuardrail:

    def test_passes_when_all_conditions_met(self):
        config = MockConfig(auto_merge=True)
        pr = make_pr()
        result = check_pr_auto_merge(pr, make_checks(), make_reviews(), config)
        assert result.passed is True

    def test_blocked_when_auto_merge_disabled_in_config(self):
        config = MockConfig(auto_merge=False)
        pr = make_pr()
        result = check_pr_auto_merge(pr, make_checks(), make_reviews(), config)
        assert result.passed is False
        assert "disabled" in result.reason.lower()

    def test_blocked_when_pr_has_conflicts(self):
        config = MockConfig(auto_merge=True)
        pr = make_pr(mergeable=False, mergeable_state="dirty")
        result = check_pr_auto_merge(pr, make_checks(), make_reviews(), config)
        assert result.passed is False
        assert "conflict" in result.reason.lower()

    def test_blocked_when_mergeability_unknown(self):
        config = MockConfig(auto_merge=True)
        pr = make_pr(mergeable=None)
        result = check_pr_auto_merge(pr, make_checks(), make_reviews(), config)
        assert result.passed is False

    def test_blocked_when_checks_failing(self):
        config = MockConfig(auto_merge=True, require_checks=True)
        pr = make_pr()
        result = check_pr_auto_merge(pr, make_checks(passing=False), make_reviews(), config)
        assert result.passed is False
        assert "check" in result.reason.lower()

    def test_blocked_when_blocking_review_exists(self):
        config = MockConfig(auto_merge=True, require_reviews=True)
        pr = make_pr()
        result = check_pr_auto_merge(pr, make_checks(), make_reviews(blocking=True), config)
        assert result.passed is False
        assert "blocked" in result.reason.lower()

    def test_blocked_for_protected_branch_by_default(self):
        config = MockConfig(auto_merge=True, allow_protected=False)
        pr = make_pr(base_ref="main")
        result = check_pr_auto_merge(pr, make_checks(), make_reviews(), config)
        assert result.passed is False
        assert "protected" in result.reason.lower()

    def test_allowed_for_protected_branch_when_explicitly_enabled(self):
        config = MockConfig(auto_merge=True, allow_protected=True)
        pr = make_pr(base_ref="main")
        result = check_pr_auto_merge(pr, make_checks(), make_reviews(), config)
        assert result.passed is True

    def test_blocked_for_draft_pr(self):
        config = MockConfig(auto_merge=True)
        pr = make_pr(draft=True)
        result = check_pr_auto_merge(pr, make_checks(), make_reviews(), config)
        assert result.passed is False
        assert "draft" in result.reason.lower()

    def test_blocked_when_no_commits(self):
        config = MockConfig(auto_merge=True)
        pr = make_pr(commits=0)
        result = check_pr_auto_merge(pr, make_checks(), make_reviews(), config)
        assert result.passed is False


# ── Tests: check_title_update ────────────────────────────────────────────────

class TestTitleUpdateGuardrail:

    def test_passes_for_non_conventional_title(self):
        config = MockConfig(polish_title=True)
        result = check_title_update("update readme", "docs: update README for V2", config)
        assert result.passed is True

    def test_blocked_when_already_conventional(self):
        config = MockConfig(polish_title=True)
        result = check_title_update("feat: add login", "feat: add authentication", config)
        assert result.passed is False

    def test_blocked_when_title_unchanged(self):
        config = MockConfig(polish_title=True)
        result = check_title_update("feat: same title", "feat: same title", config)
        assert result.passed is False

    def test_blocked_when_new_title_empty(self):
        config = MockConfig(polish_title=True)
        result = check_title_update("some title", "", config)
        assert result.passed is False

    def test_blocked_when_disabled_in_config(self):
        config = MockConfig(polish_title=False)
        result = check_title_update("update readme", "docs: update README", config)
        assert result.passed is False


# ── Tests: check_description_update ─────────────────────────────────────────

class TestDescriptionUpdateGuardrail:

    def test_passes_when_body_empty(self):
        config = MockConfig(fill_desc=True)
        result = check_description_update("", config)
        assert result.passed is True

    def test_passes_when_body_too_short(self):
        config = MockConfig(fill_desc=True)
        result = check_description_update("small", config)
        assert result.passed is True

    def test_blocked_when_body_already_substantial(self):
        config = MockConfig(fill_desc=True)
        long_body = "This PR adds authentication system with JWT tokens and Redis session management." * 2
        result = check_description_update(long_body, config)
        assert result.passed is False

    def test_blocked_when_disabled_in_config(self):
        config = MockConfig(fill_desc=False)
        result = check_description_update("", config)
        assert result.passed is False


# ── Tests: check_auto_label ──────────────────────────────────────────────────

class TestAutoLabelGuardrail:

    def test_passes_when_new_labels_to_add(self):
        config = MockConfig(auto_label=True)
        issue = {"labels": []}
        result = check_auto_label(issue, ["bug 🐛", "priority: high 🔥"], config)
        assert result.passed is True

    def test_blocked_when_labels_already_applied(self):
        config = MockConfig(auto_label=True)
        issue = {"labels": [{"name": "bug 🐛"}, {"name": "priority: high 🔥"}]}
        result = check_auto_label(issue, ["bug 🐛", "priority: high 🔥"], config)
        assert result.passed is False

    def test_blocked_when_no_labels_to_add(self):
        config = MockConfig(auto_label=True)
        issue = {"labels": []}
        result = check_auto_label(issue, [], config)
        assert result.passed is False

    def test_blocked_when_disabled_in_config(self):
        config = MockConfig(auto_label=False)
        issue = {"labels": []}
        result = check_auto_label(issue, ["bug 🐛"], config)
        assert result.passed is False

