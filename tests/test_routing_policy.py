"""
tests/test_routing_policy.py

Routing policy split out of router.py. These are pure functions over env vars
and constants, so they can be tested without constructing a router or touching
a provider — which is the point of the split.

The behaviour that matters most here is the quality floor: with
LLM_QUALITY_FLOOR=high, a user-facing task must refuse to run on a basic-tier
model rather than quietly returning a weaker review with no disclosure.
"""

from unittest.mock import patch

import pytest

from app.ai import routing_policy as P


@pytest.fixture
def env():
    with patch.dict("os.environ", {}, clear=False) as e:
        yield e


def _set(monkeypatch, **kw):
    for k, v in kw.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)


class TestTaskMap:
    def test_known_fast_task(self):
        assert P.TASK_MAP["issue_label"] == "fast"

    def test_known_deep_task(self):
        assert P.TASK_MAP["pr_analysis"] == "deep"

    def test_unknown_task_defaults_to_standard(self):
        assert P.is_quality_sensitive("some_task_that_does_not_exist") is True

    @pytest.mark.parametrize("task", ["issue_label", "commit_lint", "pr_summary"])
    def test_fast_tasks_are_not_quality_sensitive(self, task):
        assert P.is_quality_sensitive(task) is False

    @pytest.mark.parametrize("task", ["code_review", "fix_command", "pr_analysis"])
    def test_user_facing_tasks_are_quality_sensitive(self, task):
        assert P.is_quality_sensitive(task) is True


class TestProviderTier:
    @pytest.mark.parametrize("key", ["groq_70b", "gemini", "ollama"])
    def test_high_tier_providers(self, key):
        assert P.provider_tier(key) == "high"

    @pytest.mark.parametrize("key", ["groq_8b", "openrouter"])
    def test_basic_tier_providers(self, key):
        assert P.provider_tier(key) == "basic"

    def test_unknown_provider_is_treated_as_basic(self):
        """Fail safe: an unrecognised provider must not slip past the floor."""
        assert P.provider_tier("some-new-provider") == "basic"

    def test_local_ollama_counts_as_high_quality(self):
        """Running local is an explicit operator choice, not a degradation."""
        assert P.provider_tier("ollama") == "high"


class TestEnvFlags:
    def test_local_only_off_by_default(self, monkeypatch):
        _set(monkeypatch, LLM_LOCAL_ONLY=None)
        assert P.local_only() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "Yes"])
    def test_local_only_accepts_common_truthy_spellings(self, monkeypatch, value):
        _set(monkeypatch, LLM_LOCAL_ONLY=value)
        assert P.local_only() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "  "])
    def test_local_only_rejects_falsey(self, monkeypatch, value):
        _set(monkeypatch, LLM_LOCAL_ONLY=value)
        assert P.local_only() is False

    def test_prefer_local_flag(self, monkeypatch):
        _set(monkeypatch, LLM_PREFER_LOCAL="1")
        assert P.prefer_local() is True

    def test_quality_floor_only_accepts_high(self, monkeypatch):
        _set(monkeypatch, LLM_QUALITY_FLOOR="high")
        assert P.quality_floor_active() is True

    @pytest.mark.parametrize("value", ["1", "true", "medium", "yes", ""])
    def test_quality_floor_is_not_a_boolean_flag(self, monkeypatch, value):
        """It is a named level, not on/off — "1" must not enable it."""
        _set(monkeypatch, LLM_QUALITY_FLOOR=value)
        assert P.quality_floor_active() is False

    def test_quality_floor_tolerates_whitespace_and_case(self, monkeypatch):
        _set(monkeypatch, LLM_QUALITY_FLOOR="  HIGH  ")
        assert P.quality_floor_active() is True


class TestQualityFloorGate:
    def test_floor_off_allows_basic_provider_on_a_review(self, monkeypatch):
        _set(monkeypatch, LLM_QUALITY_FLOOR=None)
        assert P.blocked_by_quality_floor("groq_8b", "code_review") is False

    def test_floor_on_blocks_basic_provider_on_a_review(self, monkeypatch):
        _set(monkeypatch, LLM_QUALITY_FLOOR="high")
        assert P.blocked_by_quality_floor("groq_8b", "code_review") is True

    def test_floor_on_allows_high_tier_provider(self, monkeypatch):
        _set(monkeypatch, LLM_QUALITY_FLOOR="high")
        assert P.blocked_by_quality_floor("groq_70b", "code_review") is False

    def test_floor_never_blocks_a_fast_task(self, monkeypatch):
        """Labels and commit lint are unaffected by the floor."""
        _set(monkeypatch, LLM_QUALITY_FLOOR="high")
        assert P.blocked_by_quality_floor("groq_8b", "issue_label") is False

    def test_floor_on_blocks_unknown_provider_on_a_review(self, monkeypatch):
        _set(monkeypatch, LLM_QUALITY_FLOOR="high")
        assert P.blocked_by_quality_floor("mystery", "code_review") is True


class TestQuotasAndCost:
    def test_every_limited_provider_declares_tokens_and_requests(self):
        for key, limits in P.DAILY_LIMITS.items():
            assert "tokens" in limits, key
            assert "requests" in limits, key

    def test_local_provider_is_free(self):
        assert P.COST_PER_1K["ollama"] == 0.0

    def test_every_priced_provider_has_a_tier(self):
        """A provider that can be selected but has no tier would default to
        basic and be silently blocked by the floor."""
        for key in P.COST_PER_1K:
            assert key in P.PROVIDER_TIER, f"{key} has a price but no quality tier"

    def test_prompt_caps_are_ordered(self):
        assert P.MAX_SYSTEM_CHARS < P.MAX_USER_CHARS


class TestBackwardsCompatibleReExports:
    @pytest.mark.parametrize(
        "name",
        [
            "TASK_MAP",
            "DAILY_LIMITS",
            "COST_PER_1K",
            "PROVIDER_TIER",
            "MAX_SYSTEM_CHARS",
            "MAX_USER_CHARS",
            "QUALITY_SENSITIVE_TASK_TYPES",
        ],
    )
    def test_policy_names_still_importable_from_router(self, name):
        import app.ai.router as R

        assert getattr(R, name) is getattr(P, name)
