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
from app.ai.providers.base import (
    LLMProvider,
    LLMResponse,
    client_error_detail,
    redact_secrets,
    substituted_model,
    throttle_pause,
)

log = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Free — this provider is the emergency fallback, so reaching it must never
# start a bill.
#
# Was `mistralai/mistral-7b-instruct:free`, which the provider no longer serves:
# it appears nowhere in the 394 models OpenRouter currently lists, and there is
# no free Mistral variant at all. Every emergency fallback would have 404'd —
# the failure was invisible precisely because this path is only reached when
# things are already going wrong.
#
# Read off the provider's own catalogue, not from memory. `a12b` is the active
# parameter count of a mixture-of-experts model: 120B of knowledge at roughly
# 12B of latency, which is the right shape for a last resort. If this id is
# retired too, model_catalog substitutes the best served free model and says so
# rather than going down again.
DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"


OPENROUTER_MODEL_ENV = "LLM_OPENROUTER_MODEL"


def openrouter_model() -> str:
    """
    Overridable, and read per call -- parity with the other two providers.

    It was the only one of the three with no override at all, so pinning it
    took a code change and a deploy. That is the same trap as the model id
    itself: the setting an operator would reach for did not exist, and the
    doctor would have reported a value nothing read.
    """
    return os.environ.get(OPENROUTER_MODEL_ENV, "").strip() or DEFAULT_MODEL


class OpenRouterProvider(LLMProvider):
    """OpenRouter LLM provider — emergency fallback."""

    provider_key = "openrouter"

    def __init__(self, model: str = ""):
        from app.ai.model_catalog import effective_model

        self._model = effective_model("openrouter", model or openrouter_model())

    @property
    def api_key(self) -> str:
        """
        Read per call, like the other two providers.

        This was captured in __init__ from a module-level constant read at
        import. The router builds this provider lazily and gates it on a live
        os.environ read, so a key set after import produced a provider the
        router believed was configured and that answered `no_api_key` forever.
        """
        return os.environ.get("OPENROUTER_API_KEY", "") or OPENROUTER_API_KEY

    @property
    def model_name(self) -> str:
        return self._model

    def _post(self, body: dict, timeout: int):
        """One place that builds the request. Three copies is how a bug hides."""
        return http_requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "HTTP-Referer": "https://github.com/Shweta-Mishra-ai/github-autopilot",
                "X-Title": "GitHub Autopilot",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=timeout,
        )

    def _throttle_or_fault(self, resp, breaker, repost):
        """
        (response, stop) — `stop` is a non-None LLMResponse when the call must
        not continue.

        The third copy of a bug already fixed in the primary and the fallback:
        every non-2xx went through raise_for_status into one `except`, which
        recorded a circuit-breaker failure and returned the raw urllib message.
        So a rejected key, a retired model, an empty account and a throttle
        were indistinguishable, all four opened the breaker, and none of the
        first three can recover by waiting.

        This is the last provider the router reaches for. Opening its breaker
        on a fault that will not heal is what turns one misconfiguration into
        "all providers down".
        """
        status = getattr(resp, "status_code", 0)

        if status == 429:
            retry_after, pause = throttle_pause(resp, default=30)
            if pause:
                log.info(f"openrouter.throttled waiting={pause:g}s then retrying once")
                time.sleep(pause)
                resp = repost()
                status = getattr(resp, "status_code", 0)
            if status == 429:
                retry_after, _ = throttle_pause(resp, default=retry_after)
                breaker.record_failure(f"rate_limit retry_after={retry_after}s")
                return resp, LLMResponse(
                    text="",
                    provider="openrouter",
                    model=self._model,
                    error=f"RATE_LIMIT:{retry_after}",
                )

        if status in (400, 401, 402, 403, 404):
            # The emergency fallback pointed at a model OpenRouter had already
            # retired, so every last resort 404'd -- invisible, because this
            # path is only reached when things are going wrong already.
            replacement = substituted_model(
                "openrouter", "openrouter", "quality", self._model, resp, status
            )
            if replacement:
                self._model = replacement
                resp = repost()
                status = getattr(resp, "status_code", 0)

        if status in (400, 401, 402, 403, 404):
            detail = client_error_detail(resp, status, self._model, "OPENROUTER_API_KEY")
            log.error(f"openrouter.configuration_error status={status} model={self._model}")
            return resp, LLMResponse(
                text="",
                provider="openrouter",
                model=self._model,
                error=detail,
            )

        return resp, None

    def call_raw(
        self,
        system: str,
        user: str,
        max_tokens: int = 1000,
        temperature: float = 0.2,
        timeout: int = 30,
    ) -> str:
        """Raw API call — returns text. Used by LLMProvider.ask() base."""
        if not self.api_key:
            return ""
        resp = self._post(
            {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            timeout,
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
                text="", provider="openrouter", model=self._model, error="circuit_open"
            )

        if not self.api_key:
            return {}, LLMResponse(
                text="", provider="openrouter", model=self._model, error="no_api_key"
            )

        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }

        start = time.time()
        try:
            resp = self._post(body, timeout)
            resp, stop = self._throttle_or_fault(
                resp, breaker, lambda: self._post({**body, "model": self._model}, timeout)
            )
            if stop is not None:
                stop.latency_ms = int((time.time() - start) * 1000)
                return {}, stop

            resp.raise_for_status()
            data = resp.json()
            raw_text = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            p_tok = usage.get("prompt_tokens", 0)
            c_tok = usage.get("completion_tokens", 0)
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
            err = redact_secrets(str(e))[:100]
            breaker.record_failure(err)
            log.error(f"openrouter.ask failed: {err}")
            return {}, LLMResponse(
                text="", provider="openrouter", model=self._model, error=err, latency_ms=latency_ms
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
                text="", provider="openrouter", model=self._model, error="circuit_open"
            )

        if not self.api_key:
            return "", LLMResponse(
                text="", provider="openrouter", model=self._model, error="no_api_key"
            )

        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }

        start = time.time()
        try:
            resp = self._post(body, timeout)
            resp, stop = self._throttle_or_fault(
                resp, breaker, lambda: self._post({**body, "model": self._model}, timeout)
            )
            if stop is not None:
                stop.latency_ms = int((time.time() - start) * 1000)
                return "", stop

            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            p_tok = usage.get("prompt_tokens", 0)
            c_tok = usage.get("completion_tokens", 0)
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
            err = redact_secrets(str(e))[:100]
            breaker.record_failure(err)
            return "", LLMResponse(
                text="", provider="openrouter", model=self._model, error=err, latency_ms=latency_ms
            )
