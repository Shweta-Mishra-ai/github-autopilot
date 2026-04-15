"""
app/ai/router.py
V4 Sprint 2: Smart LLM task router.

Routes each task to the best available provider based on:
  1. Task type (fast vs deep vs long context)
  2. Live provider usage % (avoids hitting free tier limits)
  3. Circuit breaker state (skips broken providers instantly)

Usage (replaces direct groq_ask / groq_text calls):
  from app.ai.router import router

  result, meta = router.ask("system prompt", "user prompt", task="pr_analysis")
  text,   meta = router.ask_text("system", "user", task="changelog")

  # meta has: model, provider, tokens, latency_ms, cost_usd
"""

import datetime
import logging
import os
import time
from enum import Enum

from app.ai.circuit_breaker import AllProvidersDown, get_breaker
from app.ai.providers.base import LLMProvider, LLMResponse
from app.ai.providers.groq import GroqProvider

log = logging.getLogger(__name__)

# ── Task classification ───────────────────────────────────────────────────────

class TaskType(Enum):
    FAST    = "fast"     # < 500 tokens, < 1s — labels, classification, short
    STANDARD = "standard" # 500-1500 tokens, < 3s — code review, fix, explain
    DEEP    = "deep"     # 1500-3000 tokens, < 6s — PR analysis, security, arch
    LONG    = "long"     # > 3000 tokens — full file, large PR, Gemini only

# Task name → TaskType
TASK_MAP: dict[str, TaskType] = {
    # Fast (8B is enough)
    "issue_label":       TaskType.FAST,
    "commit_lint":       TaskType.FAST,
    "pr_summary":        TaskType.FAST,
    "is_duplicate":      TaskType.FAST,

    # Standard (70B preferred)
    "pr_title_rewrite":  TaskType.STANDARD,
    "code_review":       TaskType.STANDARD,
    "fix_command":       TaskType.STANDARD,
    "test_generation":   TaskType.STANDARD,
    "explain":           TaskType.STANDARD,
    "improve":           TaskType.STANDARD,
    "refactor":          TaskType.STANDARD,
    "ci_analysis":       TaskType.STANDARD,
    "gaps":              TaskType.STANDARD,
    "perf":              TaskType.STANDARD,
    "arch":              TaskType.STANDARD,
    "changelog":         TaskType.STANDARD,
    "docs":              TaskType.STANDARD,
    "budget":            TaskType.FAST,

    # Deep (70B, more tokens)
    "pr_analysis":       TaskType.DEEP,
    "security_report":   TaskType.DEEP,
    "issue_triage":      TaskType.DEEP,
    "health_report":     TaskType.DEEP,

    # Long context → Gemini
    "full_file_analysis": TaskType.LONG,
    "large_pr_review":    TaskType.LONG,
}

# Free tier daily limits
DAILY_LIMITS = {
    "groq_70b": {"tokens": 80_000,  "requests": 5_000},   # 80% of actual limit
    "groq_8b":  {"tokens": 400_000, "requests": 12_000},
    "gemini":   {"tokens": 800_000, "requests": 1_200},
}


class LLMRouter:
    """
    Central router. One instance per app (module-level singleton).
    All handlers call router.ask() / router.ask_text() — never providers directly.
    """

    def __init__(self):
        self._groq_70b = GroqProvider("llama-3.3-70b-versatile")
        self._groq_8b  = GroqProvider("llama-3.1-8b-instant")
        self._gemini   = None   # Lazy-loaded only if GEMINI_API_KEY set

    def _get_gemini(self):
        if self._gemini is None:
            try:
                from app.ai.providers.gemini import GeminiProvider
                self._gemini = GeminiProvider()
            except Exception:
                pass
        return self._gemini

    def _usage_pct(self, provider_key: str) -> float:
        """Returns 0.0-1.0 usage fraction for today."""
        try:
            from app.core.redis_client import get_redis
            r     = get_redis()
            today = datetime.date.today().isoformat()
            used  = int(r.get(f"llm:requests:{provider_key}:{today}") or 0)
            limit = DAILY_LIMITS.get(provider_key, {}).get("requests", 9999)
            return used / limit if limit else 0.0
        except Exception:
            return 0.0

    def _select_provider(self, task: str, context_tokens: int = 0) -> LLMProvider:
        """
        Select best available provider for task.
        Priority: task type → usage % → circuit state.
        """
        task_type = TASK_MAP.get(task, TaskType.STANDARD)

        # Long context → Gemini only
        if task_type == TaskType.LONG or context_tokens > 6000:
            gemini = self._get_gemini()
            if gemini and get_breaker("gemini").is_available():
                return gemini
            # Gemini unavailable → try 70B with truncation
            task_type = TaskType.DEEP

        # Fast tasks → 8B first
        if task_type == TaskType.FAST:
            if (get_breaker("groq_8b").is_available()
                    and self._usage_pct("groq_8b") < 0.85):
                return self._groq_8b
            # 8B overloaded → try Gemini
            gemini = self._get_gemini()
            if gemini and get_breaker("gemini").is_available():
                return gemini
            # Last resort: 70B
            if get_breaker("groq_70b").is_available():
                return self._groq_70b

        # Standard / Deep → 70B preferred
        groq_70b_pct = self._usage_pct("groq_70b")

        if get_breaker("groq_70b").is_available() and groq_70b_pct < 0.80:
            return self._groq_70b

        if groq_70b_pct >= 0.80:
            log.warning(f"router.groq_70b_high_usage pct={groq_70b_pct:.0%}")
            # Degrade: Standard → 8B, Deep → Gemini
            if task_type == TaskType.STANDARD and get_breaker("groq_8b").is_available():
                return self._groq_8b
            gemini = self._get_gemini()
            if gemini and get_breaker("gemini").is_available():
                return gemini

        if get_breaker("groq_8b").is_available():
            return self._groq_8b

        # Nothing available
        raise AllProvidersDown()

    def ask(
        self,
        system: str,
        user: str,
        task: str = "standard",
        max_tokens: int = 1500,
        temperature: float = 0.2,
        timeout: int = 45,
        context_tokens: int = 0,
    ) -> tuple[dict, LLMResponse]:
        """
        Route to best provider, return (parsed_json, meta).
        Handles fallback automatically.
        """
        provider   = self._select_provider(task, context_tokens)
        result, meta = provider.ask(system, user, max_tokens, temperature, timeout)

        # If primary failed, try next provider
        if meta.error and not result.get("raw"):
            log.warning(f"router.primary_failed provider={meta.provider} error={meta.error}")
            meta = self._try_fallback(
                system, user, max_tokens, temperature, timeout,
                failed_provider=meta.provider
            )
            if meta:
                result, meta = meta
            else:
                raise AllProvidersDown()

        self._log_call(task, meta)
        return result, meta

    def ask_text(
        self,
        system: str,
        user: str,
        task: str = "standard",
        max_tokens: int = 800,
        timeout: int = 30,
        context_tokens: int = 0,
    ) -> tuple[str, LLMResponse]:
        """Route to best provider, return (plain_text, meta)."""
        provider      = self._select_provider(task, context_tokens)
        text, meta    = provider.ask_text(system, user, max_tokens, timeout)

        if meta.error:
            log.warning(f"router.primary_failed provider={meta.provider} error={meta.error}")
            fallback = self._try_fallback_text(
                system, user, max_tokens, timeout,
                failed_provider=meta.provider
            )
            if fallback:
                text, meta = fallback
            else:
                raise AllProvidersDown()

        self._log_call(task, meta)
        return text, meta

    def _try_fallback(self, system, user, max_tokens, temperature, timeout,
                      failed_provider: str):
        """Try next available provider after primary fails."""
        candidates = [self._groq_70b, self._groq_8b, self._get_gemini()]
        for provider in candidates:
            if provider is None:
                continue
            if provider.provider_key == failed_provider:
                continue
            if not get_breaker(provider.provider_key).is_available():
                continue
            result, meta = provider.ask(system, user, max_tokens, temperature, timeout)
            meta.used_fallback = True
            if not meta.error:
                return result, meta
        return None

    def _try_fallback_text(self, system, user, max_tokens, timeout,
                           failed_provider: str):
        """Try next available provider for text response."""
        candidates = [self._groq_70b, self._groq_8b, self._get_gemini()]
        for provider in candidates:
            if provider is None:
                continue
            if provider.provider_key == failed_provider:
                continue
            if not get_breaker(provider.provider_key).is_available():
                continue
            text, meta = provider.ask_text(system, user, max_tokens, timeout)
            meta.used_fallback = True
            if not meta.error:
                return text, meta
        return None

    def _log_call(self, task: str, meta: LLMResponse):
        log.info(
            f"router.call task={task} provider={meta.provider} "
            f"model={meta.model} tokens={meta.total_tokens} "
            f"latency={meta.latency_ms}ms cost=${meta.cost_usd:.5f} "
            f"fallback={meta.used_fallback}"
        )

    def status(self) -> dict:
        """Used by /budget command and /health endpoint."""
        from app.ai.circuit_breaker import status_all
        today = datetime.date.today().isoformat()
        usage = {}
        try:
            from app.core.redis_client import get_redis
            r = get_redis()
            for pk, limits in DAILY_LIMITS.items():
                req_used = int(r.get(f"llm:requests:{pk}:{today}") or 0)
                tok_used = int(r.get(f"llm:tokens:{pk}:{today}") or 0)
                req_pct  = round(req_used / limits["requests"] * 100) if limits["requests"] else 0
                usage[pk] = {
                    "requests_today": req_used,
                    "requests_limit": limits["requests"],
                    "requests_pct":   req_pct,
                    "tokens_today":   tok_used,
                    "tokens_limit":   limits["tokens"],
                }
        except Exception:
            pass
        return {
            "circuit_breakers": status_all(),
            "daily_usage":      usage,
            "gemini_available": bool(os.environ.get("GEMINI_API_KEY")),
        }


# ── Module-level singleton ────────────────────────────────────────────────────
# All handlers import and use this single instance.
router = LLMRouter()
