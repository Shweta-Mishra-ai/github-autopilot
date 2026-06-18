"""
app/ai/providers/openrouter.py
OpenRouter emergency fallback provider.

OpenRouter is a proxy that supports 100+ models via a single
OpenAI-compatible API. Used as last-resort fallback when all
other providers are unavailable.

Free models available: mistralai/mistral-7b-instruct:free,
                       huggingfaceh4/zephyr-7b-beta:free
"""
import logging
import os
import time

import requests as http_requests

from app.ai.circuit_breaker import get_breaker
from app.ai.providers.base import LLMProvider, LLMResponse

log = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Free model — no cost, decent quality for fallback
DEFAULT_MODEL = "mistralai/mistral-7b-instruct:free"


class OpenRouterProvider(LLMProvider):
    """OpenRouter LLM provider — emergency fallback."""

    provider_key = "openrouter"

    def __init__(self, model: str = DEFAULT_MODEL):
        self._model = model
        self._api_key = OPENROUTER_API_KEY

    @property
    def model_name(self) -> str:
        return self._model

    def call_raw(
        self,
        system: str,
        user: str,
        max_tokens: int = 1000,
        temperature: float = 0.2,
        timeout: int = 30,
    ) -> str:
        """Raw API call — returns text. Used by LLMProvider.ask() base."""
        if not self._api_key:
            return ""
        resp = http_requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "HTTP-Referer": "https://github.com/Shweta-Mishra-ai/github-autopilot",
                "X-Title": "GitHub Autopilot",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def ask(
        self,
        system: str,
        user: str,
        max_tokens: int = 1000,
        temperature: float = 0.2,
        timeout: int = 30,
    ) -> tuple[dict, LLMResponse]:
        breaker = get_breaker("openrouter")
        if not breaker.is_available():
            return {}, LLMResponse(
                text="", provider="openrouter", model=self._model,
                error="circuit_open"
            )

        if not self._api_key:
            return {}, LLMResponse(
                text="", provider="openrouter", model=self._model,
                error="no_api_key"
            )

        start = time.time()
        try:
            resp = http_requests.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "HTTP-Referer": "https://github.com/Shweta-Mishra-ai/github-autopilot",
                    "X-Title": "GitHub Autopilot",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user",   "content": user},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "response_format": {"type": "json_object"},
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            data       = resp.json()
            raw_text   = data["choices"][0]["message"]["content"]
            usage      = data.get("usage", {})
            p_tok      = usage.get("prompt_tokens", 0)
            c_tok      = usage.get("completion_tokens", 0)
            latency_ms = int((time.time() - start) * 1000)

            from app.ai.providers.base import _extract_json as _ej
            result = _ej(raw_text)

            breaker.record_success()
            return result, LLMResponse(
                text=raw_text,
                provider="openrouter",
                model=self._model,
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                total_tokens=p_tok + c_tok,
                latency_ms=latency_ms,
            )

        except Exception as e:
            latency_ms = int((time.time() - start) * 1000)
            err = str(e)[:100]
            breaker.record_failure(err)
            log.error(f"openrouter.ask failed: {err}")
            return {}, LLMResponse(
                text="", provider="openrouter", model=self._model,
                error=err, latency_ms=latency_ms
            )

    def ask_text(
        self,
        system: str,
        user: str,
        max_tokens: int = 800,
        timeout: int = 30,
    ) -> tuple[str, LLMResponse]:
        breaker = get_breaker("openrouter")
        if not breaker.is_available():
            return "", LLMResponse(
                text="", provider="openrouter", model=self._model,
                error="circuit_open"
            )

        if not self._api_key:
            return "", LLMResponse(
                text="", provider="openrouter", model=self._model,
                error="no_api_key"
            )

        start = time.time()
        try:
            resp = http_requests.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "HTTP-Referer": "https://github.com/Shweta-Mishra-ai/github-autopilot",
                    "X-Title": "GitHub Autopilot",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user",   "content": user},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": 0.3,
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            data       = resp.json()
            text       = data["choices"][0]["message"]["content"]
            usage      = data.get("usage", {})
            p_tok      = usage.get("prompt_tokens", 0)
            c_tok      = usage.get("completion_tokens", 0)
            latency_ms = int((time.time() - start) * 1000)

            breaker.record_success()
            return text, LLMResponse(
                text=text,
                provider="openrouter",
                model=self._model,
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                total_tokens=p_tok + c_tok,
                latency_ms=latency_ms,
            )

        except Exception as e:
            latency_ms = int((time.time() - start) * 1000)
            err = str(e)[:100]
            breaker.record_failure(err)
            return "", LLMResponse(
                text="", provider="openrouter", model=self._model,
                error=err, latency_ms=latency_ms
            )

