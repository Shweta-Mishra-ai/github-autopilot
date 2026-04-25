"""
app/ai/providers/gemini.py
V4: Google Gemini Flash provider.

Circuit breaker check is the VERY FIRST thing in call_raw —
before GEMINI_API_KEY check, before any HTTP call.
"""

import logging
import os

import requests as http_requests

from app.ai.circuit_breaker import get_breaker
from app.ai.providers.base import LLMProvider, LLMResponse

log = logging.getLogger(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-1.5-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)


class GeminiProvider(LLMProvider):
    @property
    def provider_key(self) -> str:
        return "gemini"

    @property
    def model_name(self) -> str:
        return GEMINI_MODEL

    def call_raw(
        self,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
        timeout: int,
    ) -> LLMResponse:
        # ── STEP 1: Circuit breaker check — MUST be first ─────────────────────
        breaker = get_breaker("gemini")
        if not breaker.is_available():
            return LLMResponse(
                text="",
                provider="gemini",
                model=GEMINI_MODEL,
                error="Circuit OPEN for Gemini",
            )

        # ── STEP 2: API key check ─────────────────────────────────────────────
        api_key = os.environ.get("GEMINI_API_KEY", "") or GEMINI_API_KEY
        if not api_key:
            return LLMResponse(
                text="",
                provider="gemini",
                model=GEMINI_MODEL,
                error="GEMINI_API_KEY not set",
            )

        # ── STEP 3: HTTP call ─────────────────────────────────────────────────
        body = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            },
        }
        url = f"{GEMINI_URL}?key={api_key}"

        try:
            r = http_requests.post(
                url,
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            )

            if r.status_code == 429:
                breaker.record_failure("rate_limit_429")
                return LLMResponse(
                    text="",
                    provider="gemini",
                    model=GEMINI_MODEL,
                    error="RATE_LIMIT:60",
                )

            if r.status_code == 400:
                breaker.record_failure("bad_request_400")
                return LLMResponse(
                    text="",
                    provider="gemini",
                    model=GEMINI_MODEL,
                    error=f"Bad request: {r.text[:100]}",
                )

            if r.status_code >= 500:
                breaker.record_failure(f"server_error_{r.status_code}")
                return LLMResponse(
                    text="",
                    provider="gemini",
                    model=GEMINI_MODEL,
                    error=f"Server error {r.status_code}",
                )

            r.raise_for_status()
            data = r.json()

            try:
                text = data["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError) as exc:
                breaker.record_failure("bad_response_format")
                return LLMResponse(
                    text="",
                    provider="gemini",
                    model=GEMINI_MODEL,
                    error=f"Unexpected response format: {exc}",
                )

            usage = data.get("usageMetadata", {})
            p_tok = usage.get("promptTokenCount", 0)
            c_tok = usage.get("candidatesTokenCount", 0)
            t_tok = usage.get("totalTokenCount", 0)

            breaker.record_success()
            self._track(t_tok)

            return LLMResponse(
                text=text,
                provider="gemini",
                model=GEMINI_MODEL,
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                total_tokens=t_tok,
                cost_usd=0.0,
            )

        except http_requests.exceptions.Timeout:
            breaker.record_failure("timeout")
            return LLMResponse(
                text="",
                provider="gemini",
                model=GEMINI_MODEL,
                error="Request timed out",
            )
        except Exception as e:
            breaker.record_failure(str(e)[:60])
            return LLMResponse(
                text="",
                provider="gemini",
                model=GEMINI_MODEL,
                error=str(e)[:200],
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
                f"llm:tokens:gemini:{today}",
                f"llm:requests:gemini:{today}",
            ):
                r.incr(k)
                r.expire(k, 86400)
        except Exception:
            pass
