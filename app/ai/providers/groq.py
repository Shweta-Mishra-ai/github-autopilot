"""
app/ai/providers/groq.py
V4: Groq LLM provider (Llama 3.3 70B + Llama 3.1 8B).

Circuit breaker check is the VERY FIRST thing in call_raw —
before API key check, before any HTTP call.
This ensures patch.object on the breaker instance works in tests.
"""

import logging
import os

import requests as http_requests

from app.ai.circuit_breaker import get_breaker
from app.ai.providers.base import LLMProvider, LLMResponse

log = logging.getLogger(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

GROQ_COST = {
    "groq_70b": 0.0009,
    "groq_8b": 0.00006,
}


class GroqProvider(LLMProvider):
    def __init__(self, model: str = "llama-3.3-70b-versatile"):
        self._model = model

    @property
    def provider_key(self) -> str:
        if "70b" in self._model or "versatile" in self._model:
            return "groq_70b"
        return "groq_8b"

    @property
    def model_name(self) -> str:
        return self._model

    def call_raw(
        self,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
        timeout: int,
    ) -> LLMResponse:
        # ── STEP 1: Circuit breaker check — MUST be first ─────────────────────
        # patch.object on get_breaker(provider_key) instance works because
        # get_breaker() returns the same singleton from _breakers dict.
        breaker = get_breaker(self.provider_key)
        if not breaker.is_available():
            return LLMResponse(
                text="",
                provider="groq",
                model=self._model,
                error=f"Circuit OPEN for {self._model}",
            )

        # ── STEP 2: API key check ─────────────────────────────────────────────
        api_key = os.environ.get("GROQ_API_KEY", "") or GROQ_API_KEY
        if not api_key:
            return LLMResponse(
                text="",
                provider="groq",
                model=self._model,
                error="GROQ_API_KEY not set",
            )

        # ── STEP 3: HTTP call ─────────────────────────────────────────────────
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }

        try:
            r = http_requests.post(
                GROQ_URL, headers=headers, json=body, timeout=timeout
            )

            if r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", 30))
                breaker.record_failure(f"rate_limit retry_after={retry_after}s")
                return LLMResponse(
                    text="",
                    provider="groq",
                    model=self._model,
                    error=f"RATE_LIMIT:{retry_after}",
                )

            try:
                _status = int(r.status_code)
            except (TypeError, ValueError):
                _status = 0
            if _status >= 500:
                breaker.record_failure(f"server_error_{_status}")
                return LLMResponse(
                    text="",
                    provider="groq",
                    model=self._model,
                    error=f"Server error {_status}",
                )

            r.raise_for_status()
            data = r.json()
            usage = data.get("usage", {})
            p_tok = usage.get("prompt_tokens", 0)
            c_tok = usage.get("completion_tokens", 0)
            t_tok = usage.get("total_tokens", 0)
            cost = (t_tok / 1000) * GROQ_COST.get(self.provider_key, 0)
            text = data["choices"][0]["message"]["content"]

            breaker.record_success()
            self._track(t_tok)

            return LLMResponse(
                text=text,
                provider="groq",
                model=self._model,
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                total_tokens=t_tok,
                cost_usd=round(cost, 6),
            )

        except Exception as _timeout_err:
            # Catches requests.exceptions.Timeout and similar network errors
            _err_name = type(_timeout_err).__name__.lower()
            if "timeout" in _err_name or "timed out" in str(_timeout_err).lower():
                breaker.record_failure("timeout")
                return LLMResponse(
                    text="",
                    provider="groq",
                    model=self._model,
                    error="Request timed out",
                )
            raise  # re-raise non-timeout exceptions to the outer except
        except Exception as e:
            err = str(e)
            if "raise_for_status" not in err:
                breaker.record_failure(err[:60])
            return LLMResponse(
                text="",
                provider="groq",
                model=self._model,
                error=err[:200],
            )

    def _track(self, total_tokens: int):
        try:
            import datetime
            from app.core.redis_client import get_redis

            if total_tokens <= 0:
                return
            r = get_redis()
            today = datetime.date.today().isoformat()
            for k in (
                f"llm:tokens:{self.provider_key}:{today}",
                f"llm:requests:{self.provider_key}:{today}",
            ):
                r.incr(k)
                r.expire(k, 86400)
        except Exception:
            pass
