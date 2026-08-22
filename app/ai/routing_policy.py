"""
app/ai/routing_policy.py
Routing *policy*: which provider a task is allowed to reach, what it costs, and
what the operator's privacy/quality switches mean.

Split out of router.py, which mixed three concerns in one 555-line module:
this policy layer, the stateful provider registry (lazily-built clients behind
locks), and the call/retry machinery. Policy is pure — constants and env reads,
no state, no I/O — so it can be read and tested without constructing a router.

router.py re-exports every name here, so `from app.ai.router import TASK_MAP`
and friends keep working.
"""

from __future__ import annotations

import os

# ── Task → tier ──────────────────────────────────────────────────────────────
# "fast" tasks are mechanical (labels, commit lint). "standard"/"deep"/"long"
# are ones where the output IS the product and quality is visible to the user.
TASK_MAP: dict[str, str] = {
    "issue_label": "fast",
    "commit_lint": "fast",
    "pr_summary": "fast",
    "is_duplicate": "fast",
    "budget": "fast",
    "pr_title_rewrite": "standard",
    "code_review": "standard",
    "fix_command": "standard",
    "test_generation": "standard",
    "explain": "standard",
    "improve": "standard",
    "refactor": "standard",
    "ci_analysis": "standard",
    "gaps": "standard",
    "perf": "standard",
    "arch": "standard",
    "changelog": "standard",
    "docs": "standard",
    "pr_analysis": "deep",
    "security_report": "deep",
    "issue_triage": "deep",
    "health_report": "deep",
    "full_file_analysis": "long",
    "large_pr_review": "long",
}

# ── Quotas and cost ──────────────────────────────────────────────────────────
DAILY_LIMITS = {
    "groq_70b": {"tokens": 80_000, "requests": 5_000},
    "groq_8b": {"tokens": 400_000, "requests": 12_000},
    "gemini": {"tokens": 800_000, "requests": 1_200},
    "openrouter": {"tokens": 50_000, "requests": 200},
}

COST_PER_1K = {
    "groq_70b": 0.0009,
    "groq_8b": 0.00006,
    "gemini": 0.0,
    "openrouter": 0.0,
    "ollama": 0.0,  # local — free and private
}

# ── Prompt size caps ─────────────────────────────────────────────────────────
MAX_SYSTEM_CHARS = 3_000
MAX_USER_CHARS = 8_000

# ── Quality tiers ────────────────────────────────────────────────────────────
# "basic" providers are fine for fast tasks (labels, lint) but produce visibly
# weaker code reviews/fixes. Ollama counts as "high": running local is an
# explicit operator choice.
PROVIDER_TIER = {
    "groq_70b": "high",
    "gemini": "high",
    "ollama": "high",
    "groq_8b": "basic",
    "openrouter": "basic",
}

# Task types where output quality is the product (reviews, fixes, analyses).
QUALITY_SENSITIVE_TASK_TYPES = {"standard", "deep", "long"}

_TRUTHY = ("1", "true", "yes")


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def quality_floor_active() -> bool:
    """
    LLM_QUALITY_FLOOR=high → quality-sensitive tasks refuse to run on a
    basic-tier provider instead of silently degrading. Users see an honest
    "providers down, retry later" rather than an 8B model reviewing their
    code with no disclosure. Fast tasks (labels, commit lint) are unaffected.
    """
    return os.environ.get("LLM_QUALITY_FLOOR", "").strip().lower() == "high"


def local_only() -> bool:
    """
    LLM_LOCAL_ONLY=1 → NO cloud provider is ever contacted. Source code
    stays on your infrastructure. If Ollama is down, calls fail closed
    (AllProvidersDown) rather than silently leaking to a cloud provider.
    """
    return _env_flag("LLM_LOCAL_ONLY")


def prefer_local() -> bool:
    """LLM_PREFER_LOCAL=1 → try Ollama first, fall back to cloud if it fails."""
    return _env_flag("LLM_PREFER_LOCAL")


def is_quality_sensitive(task: str) -> bool:
    """True when `task` is one whose output the user reads as the product."""
    return TASK_MAP.get(task, "standard") in QUALITY_SENSITIVE_TASK_TYPES


def provider_tier(provider_key: str) -> str:
    """Quality tier of a provider. Unknown providers are treated as basic."""
    return PROVIDER_TIER.get(provider_key, "basic")


def blocked_by_quality_floor(provider_key: str, task: str) -> bool:
    """
    True when the quality floor is on, the task's output is user-facing, and
    this provider is basic-tier — i.e. the call should fail closed rather than
    quietly return weaker output.
    """
    return (
        quality_floor_active()
        and is_quality_sensitive(task)
        and provider_tier(provider_key) == "basic"
    )
