"""
app/ai/router.py
V4 Sprint 2: Smart LLM router — 4 providers, safety, cost tracking.

Provider chain (free tier, priority order):
  1. Groq 70B   — best quality, 6K req/day
  2. Groq 8B    — fast + cheap, 14K req/day
  3. Gemini Flash — long context, 1.5K req/day
  4. OpenRouter  — free models emergency fallback, 200 req/day

Safety: Input sanitization before every call.
Cost: Every call tracked in Redis → /budget shows live stats.
Context: All state in Redis (survives restarts on Render free tier).
"""

import datetime
import logging
import os

from app.ai.circuit_breaker import AllProvidersDown, get_breaker
from app.ai.providers.base import LLMProvider, LLMResponse
from app.ai.providers.groq import GroqProvider

log = logging.getLogger(__name__)

# ── Task classification ───────────────────────────────────────────────────────

TASK_MAP: dict[str, str] = {
    # fast → 8B preferred
    "issue_label":       "fast",
    "commit_lint":       "fast",
    "pr_summary":        "fast",
    "is_duplicate":      "fast",
    "budget":            "fast",

    # standard → 70B preferred
    "pr_title_rewrite":  "standard",
    "code_review":       "standard",
    "fix_command":       "standard",
    "test_generation":   "standard",
    "explain":           "standard",
    "improve":           "standard",
    "refactor":          "standard",
    "ci_analysis":       "standard",
    "gaps":              "standard",
    "perf":              "standard",
    "arch":              "standard",
    "changelog":         "standard",
    "docs":              "standard",

    # deep → 70B, more tokens
    "pr_analysis":       "deep",
    "security_report":   "deep",
    "issue_triage":      "deep",
    "health_report":     "deep",

    # long → Gemini preferred
    "full_file_analysis": "long",
    "large_pr_review":    "long",
}

# Conservative daily limits (80% of actual free tier)
DAILY_LIMITS = {
    "groq_70b":   {"tokens": 80_000,  "requests": 5_000},
    "groq_8b":    {"tokens": 400_000, "requests": 12_000},
    "gemini":     {"tokens": 800_000, "requests": 1_200},
    "openrouter": {"tokens": 50_000,  "requests": 200},
}

# Input safety limits
MAX_SYSTEM_CHARS = 3_000
MAX_USER_CHARS   = 8_000

# Cost per 1K tokens (USD) — for tracking only, Groq/Gemini/OpenRouter are $0 on free
COST_PER_1K = {
    "groq_70b":   0.0009,  # approximate — free tier is $0
    "groq_8b":    0.00006,
    "gemini":     0.0,
    "openrouter": 0.0,
}


class LLMRouter:
    """
    Central LLM router — single instance, all handlers use this.
    Handles: provider selection, safety, fallback, cost tracking.
    """

    def __init__(self):
        self._groq_70b    = GroqProvider("llama-3.3-70b-versatile")
        self._groq_8b     = GroqProvider("llama-3.1-8b-instant")
        self._gemini      = None
        self._openrouter  = None

    def _get_gemini(self):
        if self._gemini is None and os.environ.get("GEMINI_API_KEY"):
            try:
                from app.ai.providers.gemini import GeminiProvider
                self._gemini = GeminiProvider()
            except Exception:
                pass
        return self._gemini

    def _get_openrouter(self):
        if self._openrouter is None and os.environ.get("OPENROUTER_API_KEY"):
            try:
                from app.ai.providers.openrouter import OpenRouterProvider
                self._openrouter = OpenRouterProvider()
            except Exception:
                pass
        return self._openrouter

    def _usage_pct(self, provider_key: str) -> float:
        """Returns 0.0–1.0 usage fraction for today."""
        try:
            from app.core.redis_client import get_redis
            r     = get_redis()
            today = datetime.date.today().isoformat()
            used  = int(r.get(f"llm:requests:{provider_key}:{today}") or 0)
            limit = DAILY_LIMITS.get(provider_key, {}).get("requests", 9999)
            return used / limit if limit else 0.0
        except Exception:
            return 0.0

    def _sanitize(self, text: str, max_chars: int) -> str:
        """
        Safety: Remove prompt injection patterns, cap length.
        Common injection patterns in GitHub issues/comments.
        """
        if not text:
            return ""

        # Cap length
        text = text[:max_chars]

        # Remove common injection attempts
        injections = [
            "ignore previous instructions",
            "ignore all instructions",
            "disregard your system prompt",
            "you are now",
            "act as",
            "jailbreak",
            "DAN mode",
            "developer mode",
        ]
        text_lower = text.lower()
        for pattern in injections:
            if pattern in text_lower:
                # Replace with sanitized version — don't block entirely
                log.warning(f"router.injection_attempt_detected pattern='{pattern}'")
                text = text[:text_lower.index(pattern)] + "[FILTERED]" + text[text_lower.index(pattern) + len(pattern):]
                text_lower = text.lower()

        return text.strip()

    def _select_provider(self, task: str, context_tokens: int = 0) -> LLMProvider:
        """Select best available provider."""
        task_type = TASK_MAP.get(task, "standard")

        # Long context → Gemini first
        if task_type == "long" or context_tokens > 6000:
            g = self._get_gemini()
            if g and get_breaker("gemini").is_available() and self._usage_pct("gemini") < 0.85:
                return g
            task_type = "deep"  # degrade to deep

        # Fast → 8B first
        if task_type == "fast":
            if get_breaker("groq_8b").is_available() and self._usage_pct("groq_8b") < 0.85:
                return self._groq_8b
            g = self._get_gemini()
            if g and get_breaker("gemini").is_available():
                return g
            if get_breaker("groq_70b").is_available():
                return self._groq_70b

        # Standard / Deep → 70B first
        pct_70b = self._usage_pct("groq_70b")
        if get_breaker("groq_70b").is_available() and pct_70b < 0.80:
            return self._groq_70b

        # 70B busy → degrade
        if pct_70b >= 0.80:
            log.warning(f"router.groq_70b_high_usage pct={pct_70b:.0%} task={task}")

        if task_type == "standard" and get_breaker("groq_8b").is_available():
            return self._groq_8b

        g = self._get_gemini()
        if g and get_breaker("gemini").is_available():
            return g

        if get_breaker("groq_8b").is_available():
            return self._groq_8b

        # Emergency: OpenRouter
        or_p = self._get_openrouter()
        if or_p and get_breaker("openrouter").is_available():
            log.warning("router.emergency_fallback provider=openrouter")
            return or_p

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
        Route to best provider → return (parsed_json, meta).
        Input is sanitized before sending.
        Fallback to next provider on failure.
        """
        # Safety: sanitize inputs
        system = self._sanitize(system, MAX_SYSTEM_CHARS)
        user   = self._sanitize(user,   MAX_USER_CHARS)

        provider     = self._select_provider(task, context_tokens)
        result, meta = provider.ask(system, user, max_tokens, temperature, timeout)

        if meta.error:
            log.warning(f"router.primary_failed provider={meta.provider} error={meta.error} task={task}")
            fallback = self._try_fallback(system, user, max_tokens, temperature, timeout, meta.provider)
            if fallback:
                result, meta = fallback
            else:
                raise AllProvidersDown()

        self._log_and_track(task, meta)
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
        """Route to best provider → return (plain_text, meta)."""
        system = self._sanitize(system, MAX_SYSTEM_CHARS)
        user   = self._sanitize(user,   MAX_USER_CHARS)

        provider  = self._select_provider(task, context_tokens)
        text, meta = provider.ask_text(system, user, max_tokens, timeout)

        if meta.error:
            log.warning(f"router.primary_failed provider={meta.provider} error={meta.error}")
            fallback = self._try_fallback_text(system, user, max_tokens, timeout, meta.provider)
            if fallback:
                text, meta = fallback
            else:
                raise AllProvidersDown()

        self._log_and_track(task, meta)
        return text, meta

    def _try_fallback(self, system, user, max_tokens, temperature, timeout, failed_key):
        candidates = [
            self._groq_70b,
            self._groq_8b,
            self._get_gemini(),
            self._get_openrouter(),
        ]
        for p in candidates:
            if p is None or p.provider_key == failed_key:
                continue
            if not get_breaker(p.provider_key).is_available():
                continue
            result, meta = p.ask(system, user, max_tokens, temperature, timeout)
            meta.used_fallback = True
            if not meta.error:
                return result, meta
        return None

    def _try_fallback_text(self, system, user, max_tokens, timeout, failed_key):
        candidates = [
            self._groq_70b,
            self._groq_8b,
            self._get_gemini(),
            self._get_openrouter(),
        ]
        for p in candidates:
            if p is None or p.provider_key == failed_key:
                continue
            if not get_breaker(p.provider_key).is_available():
                continue
            text, meta = p.ask_text(system, user, max_tokens, timeout)
            meta.used_fallback = True
            if not meta.error:
                return text, meta
        return None

    def _log_and_track(self, task: str, meta: LLMResponse):
        """Log call + track cost in Redis."""
        cost_est = (meta.total_tokens / 1000) * COST_PER_1K.get(meta.provider, 0)

        log.info(
            f"router.call task={task} provider={meta.provider} "
            f"model={meta.model.split('/')[-1]} "
            f"tokens={meta.total_tokens} latency={meta.latency_ms}ms "
            f"cost_est=${cost_est:.5f} fallback={meta.used_fallback}"
        )

        # Track cost in Redis
        try:
            from app.core.redis_client import get_redis
            r     = get_redis()
            today = datetime.date.today().isoformat()
            # Store cost in millicents (int) to avoid float precision issues
            cost_mc = int(cost_est * 100_000)
            if cost_mc > 0:
                r.incr(f"llm:cost_mc:{meta.provider}:{today}")
                r.expire(f"llm:cost_mc:{meta.provider}:{today}", 86400)
        except Exception:
            pass

    def status(self) -> dict:
        from app.ai.circuit_breaker import status_all
        today = datetime.date.today().isoformat()
        usage = {}
        try:
            from app.core.redis_client import get_redis
            r = get_redis()
            for pk, limits in DAILY_LIMITS.items():
                req  = int(r.get(f"llm:requests:{pk}:{today}") or 0)
                tok  = int(r.get(f"llm:tokens:{pk}:{today}") or 0)
                cost = int(r.get(f"llm:cost_mc:{pk}:{today}") or 0) / 100_000
                usage[pk] = {
                    "requests_today": req,
                    "requests_pct":   round(req / limits["requests"] * 100) if limits["requests"] else 0,
                    "tokens_today":   tok,
                    "cost_usd_today": round(cost, 5),
                }
        except Exception:
            pass

        return {
            "circuit_breakers": status_all(),
            "daily_usage":      usage,
            "providers_enabled": {
                "groq":        bool(os.environ.get("GROQ_API_KEY")),
                "gemini":      bool(os.environ.get("GEMINI_API_KEY")),
                "openrouter":  bool(os.environ.get("OPENROUTER_API_KEY")),
            },
        }


# Module-level singleton — all handlers import this
router = LLMRouter()
