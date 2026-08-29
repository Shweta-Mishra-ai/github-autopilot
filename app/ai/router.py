"""
app/ai/router.py
Thread-safe LLM router, 4 providers, safety, cost tracking.

Provider chain (free tier):
  1. Groq 70B   — best quality
  2. Groq 8B    — fast
  3. Gemini Flash — long context
  4. OpenRouter  — emergency fallback

Routing *policy* — task tiers, quotas, cost, and the operator privacy/quality
switches — lives in app/ai/routing_policy.py. This module owns the stateful
half: lazily-built provider clients behind locks, selection, retry, fallback
and telemetry. Every policy name is re-exported below, so existing imports
(`from app.ai.router import TASK_MAP`) are unaffected.
"""

import datetime
import logging
import os
import threading

from app.ai.circuit_breaker import AllProvidersDown, get_breaker
from app.ai.providers.base import LLMProvider, LLMResponse
from app.ai.providers.groq import GroqProvider
from app.ai.routing_policy import (
    COST_PER_1K,
    DAILY_LIMITS,
    MAX_SYSTEM_CHARS,
    MAX_USER_CHARS,
    PROVIDER_TIER,
    QUALITY_SENSITIVE_TASK_TYPES,
    TASK_MAP,
    blocked_by_quality_floor,
    local_only,
    prefer_local,
    quality_floor_active,
)

log = logging.getLogger(__name__)

# Model ids the deployment asks for when nothing overrides them. Named rather
# than inline so the eval preflight and /health report the same values the
# router actually calls — a check that validates a different model than the one
# in use is worse than no check.
#
# These were llama-3.3-70b-versatile and llama-3.1-8b-instant. The provider
# retired every Llama chat model, so both returned 404 and every AI command
# failed for six days. The replacements below are not a guess: they are from
# the provider's own model list, printed by the eval preflight on the
# 2026-08-28 run using this deployment's key. That list contained no Llama chat
# model at all; the remaining general-purpose ones are the gpt-oss and qwen
# families, plus Groq's compound systems.
#
# The pairing keeps the shape the router expects — a larger model for work that
# needs quality, a smaller one to fall back to and to carry cheap traffic.
#
# A provider will retire these too. LLM_PRIMARY_MODEL and LLM_FALLBACK_MODEL
# override them without a deploy, /health reports which are in use, and the
# nightly eval names what is available when they stop existing.
DEFAULT_PRIMARY_MODEL = "openai/gpt-oss-120b"
DEFAULT_FALLBACK_MODEL = "openai/gpt-oss-20b"


__all__ = [
    "LLMRouter",
    "router",
    "COST_PER_1K",
    "DAILY_LIMITS",
    "MAX_SYSTEM_CHARS",
    "MAX_USER_CHARS",
    "PROVIDER_TIER",
    "QUALITY_SENSITIVE_TASK_TYPES",
    "TASK_MAP",
    "blocked_by_quality_floor",
    "last_model_disclosure",
    "reset_last_call",
]


# Backwards-compatible alias — this was a module-level private in router.py and
# is referenced by name in tests.
def _quality_floor_active() -> bool:
    return quality_floor_active()


class LLMRouter:
    def __init__(self):
        # Model choice is a DEPLOYMENT concern, not a per-repo one: this
        # router is a process-wide singleton serving every installation, so a
        # repo-level override would let one tenant pick a model that drains
        # another's quota tier. It was previously declared in repo config
        # (ai.primary_model) where nothing could read it.
        # The tier is passed explicitly. Inferring it from the model id tied
        # the budget and the circuit breaker to a naming convention the
        # provider was free to change -- and did.
        self._groq_70b = GroqProvider(
            os.environ.get("LLM_PRIMARY_MODEL", DEFAULT_PRIMARY_MODEL), provider_key="groq_70b"
        )
        self._groq_8b = GroqProvider(
            os.environ.get("LLM_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL), provider_key="groq_8b"
        )
        self._gemini = None
        self._openrouter = None
        self._ollama = None
        self._gemini_lock = threading.Lock()
        self._openrouter_lock = threading.Lock()
        self._ollama_lock = threading.Lock()

    # Thin delegations to routing_policy so the env-var contract lives in one
    # place; kept as methods because call sites and tests reference them here.
    @staticmethod
    def _local_only() -> bool:
        return local_only()

    @staticmethod
    def _prefer_local() -> bool:
        return prefer_local()

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
        from app.core.sanitizer import InjectionRejected

        try:
            from app.core.sanitizer import sanitize_user_input

            return sanitize_user_input(text)
        except InjectionRejected:
            # A critical-severity injection attempt. Must propagate — the whole
            # point of fail-closed is that the request does not proceed.
            raise
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
            self._note_configuration_error(meta.error)
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
            self._note_configuration_error(meta.error)
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
        # One predicate rather than re-deriving "floor on AND task sensitive AND
        # provider basic" at each site — the two copies could drift apart.
        candidates = [
            p
            for p in candidates
            if p is not None and not blocked_by_quality_floor(p.provider_key, task)
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

        # Feed the degraded-mode tracker. app/core/health_check.py implements
        # per-provider latency stats and a "provider is slow" message, but
        # record_latency() had no callers, so get_system_health() was computed
        # from an empty dataset and always reported healthy. The feature was
        # built and then never connected to anything that knows a latency.
        try:
            from app.core.health_check import record_latency

            record_latency(meta.provider, int(meta.latency_ms or 0))
        except Exception:
            pass  # health tracking must never affect request

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
        except Exception as e:
            log.debug(f"router.budget_alert_check_failed provider={provider_key}: {e}")

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
            config_fault = self.configuration_error()
            if config_fault:
                # "Providers are down, try again shortly" is wrong and costly
                # here: nothing is down and waiting will not help. Say what is
                # actually misconfigured so the reader fixes it instead of
                # retrying for a day.
                log.error(f"router.configuration_fault {config_fault}")
            return (
                {
                    "_providers_down": True,
                    "retry_in": e.retry_in_seconds,
                    "message": config_fault or degraded_message,
                    "_configuration_error": bool(config_fault),
                },
                None,
            )

    # A configuration fault is remembered, not just logged: it is discovered on
    # a webhook thread and needs to be readable from /health and the doctor,
    # which run on a different request entirely.
    _config_error: str = ""

    def _note_configuration_error(self, error: str) -> None:
        from app.ai.providers.base import is_configuration_error

        if is_configuration_error(error):
            LLMRouter._config_error = str(error)

    def configuration_error(self) -> str:
        """The last permanent provider misconfiguration seen, or ""."""
        return LLMRouter._config_error

    def clear_configuration_error(self) -> None:
        LLMRouter._config_error = ""

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
        except Exception as e:
            log.debug(f"router.status_usage_fetch_failed: {e}")
        return {
            "circuit_breakers": status_all(),
            "daily_usage": usage,
            "providers_enabled": {
                "groq": bool(os.environ.get("GROQ_API_KEY")),
                "gemini": bool(os.environ.get("GEMINI_API_KEY")),
                "openrouter": bool(os.environ.get("OPENROUTER_API_KEY")),
            },
            "models": {
                "primary": os.environ.get("LLM_PRIMARY_MODEL", DEFAULT_PRIMARY_MODEL),
                "fallback": os.environ.get("LLM_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL),
            },
            # Surfaced here because the fault is discovered on a webhook thread
            # and read from /health on a different request. Open breakers alone
            # cannot distinguish "the provider is having a bad hour" from "the
            # model id is wrong", and only one of those is worth waiting out.
            "configuration_error": self.configuration_error(),
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
