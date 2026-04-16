"""
app/ai/providers/gemini.py
V4 Sprint 2: Google Gemini Flash provider.

Free tier: 1,500 req/day, 1M tokens/day — perfect for long-context fallback.
Best for: PRs with many files, full file analysis, large thread summaries.

Setup: GEMINI_API_KEY env var (Google AI Studio — free).
Get key: https://aistudio.google.com/app/apikey
"""

import logging
import os

import requests as http_requests

from app.ai.providers.base import LLMProvider, LLMResponse
from app.ai.circuit_breaker import get_breaker

log = logging.getLogger(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL   = "gemini-1.5-flash"
GEMINI_URL     = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

# Gemini Flash free tier cost = $0
GEMINI_COST_PER_1K = 0.0


class GeminiProvider(LLMProvider):
    """Google Gemini Flash provider — best for long context (up to 1M tokens)."""

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
        breaker = get_breaker("gemini")

        if not breaker.is_available():
            return LLMResponse(
                text="", provider="gemini", model=GEMINI_MODEL,
                error="Circuit OPEN for Gemini",
            )

        if not GEMINI_API_KEY:
            return LLMResponse(
                text="", provider="gemini", model=GEMINI_MODEL,
                error="GEMINI_API_KEY not set",
            )

        # Gemini API format — system instruction + user message
        body = {
            "system_instruction": {
                "parts": [{"text": system}]
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": user}]
                }
            ],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature":     temperature,
            },
        }

        url = f"{GEMINI_URL}?key={GEMINI_API_KEY}"

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
                    text="", provider="gemini", model=GEMINI_MODEL,
                    error="RATE_LIMIT:60",
                )

            if r.status_code == 400:
                # Bad request — usually content safety block
                breaker.record_failure("bad_request_400")
                return LLMResponse(
                    text="", provider="gemini", model=GEMINI_MODEL,
                    error=f"Bad request: {r.text[:100]}",
                )

            if r.status_code >= 500:
                breaker.record_failure(f"server_error_{r.status_code}")
                return LLMResponse(
                    text="", provider="gemini", model=GEMINI_MODEL,
                    error=f"Server error {r.status_code}",
                )

            r.raise_for_status()
            data = r.json()

            # Extract text from Gemini response format
            try:
                text = data["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError) as e:
                breaker.record_failure("bad_response_format")
                return LLMResponse(
                    text="", provider="gemini", model=GEMINI_MODEL,
                    error=f"Unexpected response format: {e}",
                )

            # Token usage (Gemini provides usageMetadata)
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
                cost_usd=0.0,  # Free tier
            )

        except http_requests.exceptions.Timeout:
            breaker.record_failure("timeout")
            return LLMResponse(
                text="", provider="gemini", model=GEMINI_MODEL,
                error="Request timed out",
            )
        except Exception as e:
            breaker.record_failure(str(e)[:60])
            return LLMResponse(
                text="", provider="gemini", model=GEMINI_MODEL,
                error=str(e)[:200],
            )

    def _track(self, total_tokens: int):
        """Track Gemini usage in Redis."""
        try:
            import datetime
            from app.core.redis_client import get_redis
            if total_tokens <= 0:
                return
            r     = get_redis()
            today = datetime.date.today().isoformat()
            for k in (
                f"llm:tokens:gemini:{today}",
                f"llm:requests:gemini:{today}",
            ):
                r.incr(k)
                r.expire(k, 86400)
        except Exception:
            pass
