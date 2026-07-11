"""
app/ai/router.py
Thread-safe LLM router, 4 providers, safety, cost tracking.

Provider chain (free tier):
  1. Groq 70B   — best quality
  2. Groq 8B    — fast
  3. Gemini Flash — long context
  4. OpenRouter  — emergency fallback
"""

import datetime
import logging
import os
import threading

from app.ai.circuit_breaker import AllProvidersDown, get_breaker
from app.ai.providers.base import LLMProvider, LLMResponse
from app.ai.providers.groq import GroqProvider

log = logging.getLogger(__name__)

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

DAILY_LIMITS = {
    "groq_70b": {"tokens": 80_000, "requests": 5_000},
    "groq_8b": {"tokens": 400_000, "requests": 12_000},
    "gemini": {"tokens": 800_000, "requests": 1_200},
    "openrouter": {"tokens": 50_000, "requests": 200},
}

MAX_SYSTEM_CHARS = 3_000
MAX_USER_CHARS = 8_000

COST_PER_1K = {
    "groq_70b": 0.0009,
    "groq_8b": 0.00006,
    "gemini": 0.0,
    "openrouter": 0.0,
    "ollama": 0.0,  # local — free and private
}

# Quality tiers for the LLM_QUALITY_FLOOR guard. "basic" providers are fine
# for fast tasks (labels, lint) but produce visibly weaker code reviews/fixes.
# Ollama counts as "high": running local is an explicit operator choice.
PROVIDER_TIER = {
    "groq_70b": "high",
    "gemini": "high",
    "ollama": "high",
    "groq_8b": "basic",
    "openrouter": "basic",
}

# Task types where output quality is the product (reviews, fixes, analyses).
QUALITY_SENSITIVE_TASK_TYPES = {"standard", "deep", "long"}


def _quality_floor_active() -> bool:
    """
    LLM_QUALITY_FLOOR=high → quality-sensitive tasks refuse to run on a
    basic-tier provider instead of silently degrading. Users see an honest
    "providers down, retry later" rather than an 8B model reviewing their
    code with no disclosure. Fast tasks (labels, commit lint) are unaffected.
    """
    return os.environ.get("LLM_QUALITY_FLOOR", "").strip().lower() == "high"


class LLMRouter:
    def __init__(self):
        self._groq_70b = GroqProvider("llama-3.3-70b-versatile")
        self._groq_8b = GroqProvider("llama-3.1-8b-instant")
        self._gemini = None
        self._openrouter = None
        self._ollama = None
        self._gemini_lock = threading.Lock()
        self._openrouter_lock = threading.Lock()
        self._ollama_lock = threading.Lock()

    @staticmethod
    def _local_only() -> bool:
        """
        LLM_LOCAL_ONLY=1 → NO cloud provider is ever contacted. Source code
        stays on your infrastructure. If Ollama is down, calls fail closed
        (AllProvidersDown) rather than silently leaking to a cloud provider.
        """
        return os.environ.get("LLM_LOCAL_ONLY", "").strip().lower() in ("1", "true", "yes")

    @staticmethod
    def _prefer_local() -> bool:
        """LLM_PREFER_LOCAL=1 → try Ollama first, fall back to cloud if it fails."""
        return os.environ.get("LLM_PREFER_LOCAL", "").strip().lower() in ("1", "true", "yes")

    def _get_ollama(self) -> "LLMProvider | None":
        from app.ai.providers.ollama import is_configured

        if not is_configured():
            return None
        if self._ollama is not None:
            return self._ollama
        with self._ollama_lock:
            if self._ollama is None:
                try:
                    from app.ai.providers.ollama import OllamaProvider

                    self._ollama = OllamaProvider()
                except Exception as e:
                    log.warning(f"router.ollama_init_failed: {e}")
        return self._ollama

    def _get_gemini(self) -> "LLMProvider | None":
        if self._gemini is not None:
            return self._gemini
        with self._gemini_lock:
            if self._gemini is None and os.environ.get("GEMINI_API_KEY"):
                try:
                    from app.ai.providers.gemini import GeminiProvider

                    self._gemini = GeminiProvider()
                except Exception as e:
                    log.warning(f"router.gemini_init_failed: {e}")
        return self._gemini

    def _get_openrouter(self) -> "LLMProvider | None":
        if self._openrouter is not None:
            return self._openrouter
        with self._openrouter_lock:
            if self._openrouter is None and os.environ.get("OPENROUTER_API_KEY"):
                try:
                    from app.ai.providers.openrouter import OpenRouterProvider

                    self._openrouter = OpenRouterProvider()
                except Exception as e:
                    log.warning(f"router.openrouter_init_failed: {e}")
        return self._openrouter

    def _usage_pct(self, provider_key: str) -> float:
        try:
            from app.core.redis_client import get_redis

            r = get_redis()
            today = datetime.date.today().isoformat()
            used = int(r.get(f"llm:requests:{provider_key}:{today}") or 0)
            limit = DAILY_LIMITS.get(provider_key, {}).get("requests", 9999)
            return used / limit if limit else 0.0
        except Exception:
            return 0.0

    def _sanitize(self, text: str, max_chars: int) -> str:
        """
        Sanitize user input before sending to LLM.
        Truncates first, then runs injection filter.
        Uses structured delimiters to separate user content from instructions.
        """
        if not text:
            return ""
        text = text[:max_chars]
        try:
            from app.core.sanitizer import sanitize_user_input

            return sanitize_user_input(text)
        except Exception:
            # Fallback: basic injection filter
            for pattern in [
                "ignore all previous",
                "you are now",
                "jailbreak",
                "disregard",
                "forget your instructions",
            ]:
                lower = text.lower()
                if pattern in lower:
                    idx = lower.index(pattern)
                    text = text[:idx] + "[FILTERED]" + text[idx + len(pattern) :]
            return text

    def _select_provider(self, task: str, context_tokens: int = 0) -> LLMProvider:
        """
        Select best available provider.
        GUARANTEED to raise AllProvidersDown if nothing is available.
        """
        task_type = TASK_MAP.get(task, "standard")

        # ── Privacy modes — evaluated before any cloud provider ────────────────
        # LLM_LOCAL_ONLY: Ollama or nothing. Never leaks code to a cloud API.
        if self._local_only():
            ollama = self._get_ollama()
            if ollama and get_breaker("ollama").is_available():
                return ollama
            raise AllProvidersDown()

        # LLM_PREFER_LOCAL: try local first, but cloud fallback is allowed.
        if self._prefer_local():
            ollama = self._get_ollama()
            if ollama and get_breaker("ollama").is_available():
                return ollama

        # Long context → Gemini first
        if task_type == "long" or context_tokens > 6000:
            g = self._get_gemini()
            if g and get_breaker("gemini").is_available() and self._usage_pct("gemini") < 0.85:
                return g
            task_type = "deep"

        # Fast → 8B first
        if task_type == "fast":
            if get_breaker("groq_8b").is_available() and self._usage_pct("groq_8b") < 0.85:
                return self._groq_8b
            g = self._get_gemini()
            if g and get_breaker("gemini").is_available():
                return g
            if get_breaker("groq_70b").is_available():
                return self._groq_70b
            or_p = self._get_openrouter()
            if or_p and get_breaker("openrouter").is_available():
                return or_p
            raise AllProvidersDown()

        # Standard / Deep → 70B first
        floor = _quality_floor_active() and task_type in QUALITY_SENSITIVE_TASK_TYPES

        pct_70b = self._usage_pct("groq_70b")
        if get_breaker("groq_70b").is_available() and pct_70b < 0.80:
            return self._groq_70b

        if pct_70b >= 0.80:
            log.warning(f"router.groq_70b_high_usage pct={pct_70b:.0%} task={task}")

        if task_type == "standard" and not floor and get_breaker("groq_8b").is_available():
            return self._groq_8b

        g = self._get_gemini()
        if g and get_breaker("gemini").is_available():
            return g

        if floor:
            # Only basic-tier providers remain. Refuse honestly instead of
            # letting an 8B model review code with no disclosure.
            log.warning(
                f"router.quality_floor_refusal task={task} — no high-tier provider available"
            )
            raise AllProvidersDown()

        if get_breaker("groq_8b").is_available():
            return self._groq_8b

        or_p = self._get_openrouter()
        if or_p and get_breaker("openrouter").is_available():
            log.warning("router.emergency_fallback provider=openrouter")
            return or_p

        raise AllProvidersDown()

    def _call_provider(
        self,
        provider: LLMProvider,
        system: str,
        user: str,
        max_tokens: int = 1500,
        temperature: float = 0.2,
        timeout: int = 45,
    ) -> tuple[dict, LLMResponse]:
        """Call a specific provider. Used by tests to patch individual calls."""
        return provider.ask(system, user, max_tokens, temperature, timeout)

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
        system = self._sanitize(system, MAX_SYSTEM_CHARS)
        user = self._sanitize(user, MAX_USER_CHARS)
        provider = self._select_provider(task, context_tokens)
        resp = self._call_provider(provider, system, user, max_tokens, temperature, timeout)
        if isinstance(resp, tuple):
            result, meta = resp
        else:
            meta = resp
            if meta.error:
                result = {"error": meta.error}
            else:
                from app.ai.providers.base import _extract_json

                result = _extract_json(meta.text)

        if meta.error:
            log.warning(f"router.primary_failed provider={meta.provider} error={meta.error}")
            fallback = self._try_fallback(
                system, user, max_tokens, temperature, timeout, meta.provider, task
            )
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
        system = self._sanitize(system, MAX_SYSTEM_CHARS)
        user = self._sanitize(user, MAX_USER_CHARS)
        provider = self._select_provider(task, context_tokens)
        text, meta = provider.ask_text(system, user, max_tokens, timeout)

        if meta.error:
            fallback = self._try_fallback_text(
                system, user, max_tokens, timeout, meta.provider, task
            )
            if fallback:
                text, meta = fallback
            else:
                raise AllProvidersDown()

        self._log_and_track(task, meta)
        return text, meta

    def _fallback_candidates(self, task: str = "standard") -> list:
        """
        Provider order for fallback. In LLM_LOCAL_ONLY mode the list contains
        ONLY Ollama — cloud providers are never appended, so a local failure
        can never silently leak code to a cloud API.

        When LLM_QUALITY_FLOOR=high, basic-tier providers are excluded for
        quality-sensitive tasks — the same guarantee _select_provider gives
        must hold on the fallback path too.
        """
        if self._local_only():
            return [self._get_ollama()]
        candidates = [self._groq_70b, self._groq_8b, self._get_gemini(), self._get_openrouter()]
        if self._prefer_local():
            candidates.insert(0, self._get_ollama())
        task_type = TASK_MAP.get(task, "standard")
        if _quality_floor_active() and task_type in QUALITY_SENSITIVE_TASK_TYPES:
            candidates = [
                p
                for p in candidates
                if p is not None and PROVIDER_TIER.get(p.provider_key, "basic") == "high"
            ]
        return candidates

    def _try_fallback(
        self, system, user, max_tokens, temperature, timeout, failed_key, task="standard"
    ):
        candidates = self._fallback_candidates(task)
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

    def _try_fallback_text(self, system, user, max_tokens, timeout, failed_key, task="standard"):
        candidates = self._fallback_candidates(task)
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
        # Remembered per-thread so comment assembly can disclose which model
        # actually produced the output (handlers run one event per thread).
        _last_call.provider = meta.provider
        _last_call.model = meta.model
        cost_est = (meta.total_tokens / 1000) * COST_PER_1K.get(meta.provider, 0)
        log.info(
            f"router.call task={task} provider={meta.provider} "
            f"tokens={meta.total_tokens} latency={meta.latency_ms}ms "
            f"cost=${cost_est:.5f} fallback={meta.used_fallback}"
        )
        try:
            from app.core.redis_client import get_redis

            r = get_redis()
            today = datetime.date.today().isoformat()
            cost_mc = int(cost_est * 100_000)
            if cost_mc > 0:
                cost_key = f"llm:cost_mc:{meta.provider}:{today}"
                r.incrby(cost_key, cost_mc)
                r.expire(cost_key, 86400)

            req_key = f"llm:requests:{meta.provider}:{today}"
            r.incr(req_key)
            r.expire(req_key, 86400)

            tok_key = f"llm:tokens:{meta.provider}:{today}"
            r.incrby(tok_key, meta.total_tokens)
            r.expire(tok_key, 86400)

            self._check_budget_alert(r, meta.provider, today)
        except Exception:
            pass  # tracking must never affect request

    def _check_budget_alert(self, r, provider_key: str, today: str):
        try:
            limits = DAILY_LIMITS.get(provider_key, {})
            token_limit = limits.get("tokens", 0)
            if not token_limit:
                return
            used = int(r.get(f"llm:tokens:{provider_key}:{today}") or 0)
            pct = used / token_limit
            if pct >= 0.80:
                log.warning(
                    f"router.budget_alert provider={provider_key} "
                    f"tokens_used={used} limit={token_limit} pct={pct:.0%}"
                )
        except Exception:
            pass

    def safe_ask(
        self,
        system: str,
        user: str,
        task: str = "standard",
        max_tokens: int = 1500,
        temperature: float = 0.2,
        timeout: int = 45,
        context_tokens: int = 0,
        degraded_message: str = "",
    ) -> tuple[dict, "LLMResponse | None"]:
        """
        Like ask() but never raises — returns (degraded_dict, None) when all
        providers are down.
        """
        try:
            return self.ask(system, user, task, max_tokens, temperature, timeout, context_tokens)
        except AllProvidersDown as e:
            log.error(f"router.all_providers_down task={task} retry_in={e.retry_in_seconds}s")
            return (
                {
                    "_providers_down": True,
                    "retry_in": e.retry_in_seconds,
                    "message": degraded_message,
                },
                None,
            )

    def status(self) -> dict:
        from app.ai.circuit_breaker import status_all

        today = datetime.date.today().isoformat()
        usage = {}
        try:
            from app.core.redis_client import get_redis

            r = get_redis()
            for pk, limits in DAILY_LIMITS.items():
                req = int(r.get(f"llm:requests:{pk}:{today}") or 0)
                tok = int(r.get(f"llm:tokens:{pk}:{today}") or 0)
                cost = int(r.get(f"llm:cost_mc:{pk}:{today}") or 0) / 100_000
                usage[pk] = {
                    "requests_today": req,
                    "requests_pct": round(req / limits["requests"] * 100)
                    if limits["requests"]
                    else 0,
                    "tokens_today": tok,
                    "cost_usd_today": round(cost, 5),
                }
        except Exception:
            pass
        return {
            "circuit_breakers": status_all(),
            "daily_usage": usage,
            "providers_enabled": {
                "groq": bool(os.environ.get("GROQ_API_KEY")),
                "gemini": bool(os.environ.get("GEMINI_API_KEY")),
                "openrouter": bool(os.environ.get("OPENROUTER_API_KEY")),
            },
        }


# Per-thread record of the last completed LLM call (provider + model).
_last_call = threading.local()


def reset_last_call() -> None:
    """Clear this thread's model record (call at the start of each event)."""
    _last_call.provider = ""
    _last_call.model = ""


def last_model_disclosure() -> str:
    """
    Human-readable "which model wrote this" line for bot output, from the
    last completed LLM call on this thread. Empty string when nothing has
    run yet — callers append it blindly.
    """
    provider = getattr(_last_call, "provider", "")
    model = getattr(_last_call, "model", "")
    if not provider:
        return ""
    return f" · model: `{model or provider}`"


# Module-level singleton — thread-safe via instance locks above
router = LLMRouter()
