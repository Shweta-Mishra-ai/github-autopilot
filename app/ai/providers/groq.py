"""
app/ai/providers/groq.py
V4 Sprint 2: Groq LLM provider (Llama 3.3 70B + Llama 3.1 8B).

Free tier limits (daily):
  70B: ~6,000 requests, ~100K tokens
  8B:  ~14,400 requests, ~500K tokens
"""

import logging
import os

import requests as http_requests

from app.ai.providers.base import LLMProvider, LLMResponse
from app.ai.circuit_breaker import get_breaker

log = logging.getLogger(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"

# Cost per 1K tokens (USD) — Groq free tier is $0 but tracking for awareness
GROQ_COST = {
    "groq_70b": 0.0009,
    "groq_8b":  0.00006,
}


class GroqProvider(LLMProvider):
    """Groq API provider. Supports Llama 70B and 8B."""

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
        breaker = get_breaker(self.provider_key)

        if not breaker.is_available():
            return LLMResponse(
                text="", provider="groq", model=self._model,
                error=f"Circuit OPEN for {self._model}",
            )

        if not GROQ_API_KEY:
            return LLMResponse(
                text="", provider="groq", model=self._model,
                error="GROQ_API_KEY not set",
            )

        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type":  "application/json",
        }
        body = {
            "model":       self._model,
            "max_tokens":  max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
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
                    text="", provider="groq", model=self._model,
                    error=f"RATE_LIMIT:{retry_after}",
                )

            if r.status_code >= 500:
                breaker.record_failure(f"server_error_{r.status_code}")
                return LLMResponse(
                    text="", provider="groq", model=self._model,
                    error=f"Server error {r.status_code}",
                )

            r.raise_for_status()
            data = r.json()

            usage = data.get("usage", {})
            p_tok = usage.get("prompt_tokens", 0)
            c_tok = usage.get("completion_tokens", 0)
            t_tok = usage.get("total_tokens", 0)
            cost  = (t_tok / 1000) * GROQ_COST.get(self.provider_key, 0)

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

        except http_requests.exceptions.Timeout:
            breaker.record_failure("timeout")
            return LLMResponse(
                text="", provider="groq", model=self._model,
                error="Request timed out",
            )
        except Exception as e:
            breaker.record_failure(str(e)[:60])
            return LLMResponse(
                text="", provider="groq", model=self._model,
                error=str(e)[:200],
            )

    def _track(self, total_tokens: int):
        """Track usage in Redis for /budget command."""
        try:
            import datetime
            from app.core.redis_client import get_redis
            if total_tokens <= 0:
                return
            r     = get_redis()
            today = datetime.date.today().isoformat()
            for k in (
                f"llm:tokens:{self.provider_key}:{today}",
                f"llm:requests:{self.provider_key}:{today}",
            ):
                r.incr(k)
                r.expire(k, 86400)
        except Exception:
            pass
