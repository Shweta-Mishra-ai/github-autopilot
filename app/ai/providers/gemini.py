"""
app/ai/providers/gemini.py
V4: Google Gemini Flash provider.

Circuit breaker check is the VERY FIRST thing in call_raw —
before GEMINI_API_KEY check, before any HTTP call.
"""

import logging
import os
import time

import requests as http_requests

from app.ai.circuit_breaker import get_breaker
from app.ai.model_catalog import effective_model
from app.ai.providers.base import (
    LLMProvider,
    LLMResponse,
    client_error_detail,
    redact_secrets,
    substituted_model,
    throttle_pause,
)

log = logging.getLogger(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Overridable, and read per call. The model id was hardcoded, so when the
# provider retires it — which is exactly what just happened to this
# deployment's Groq models — the only fix would be a code change and a deploy.
# The primary provider learned that lesson (LLM_PRIMARY_MODEL); the fallback
# had not, which is the worse place for it: the fallback is what you reach for
# when the primary is already broken.
DEFAULT_GEMINI_MODEL = "gemini-1.5-flash"
GEMINI_MODEL_ENV = "LLM_GEMINI_MODEL"


def gemini_model() -> str:
    return os.environ.get(GEMINI_MODEL_ENV, "").strip() or DEFAULT_GEMINI_MODEL


def _gemini_url(model: str) -> str:
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiProvider(LLMProvider):
    @property
    def provider_key(self) -> str:
        return "gemini"

    @property
    def model_name(self) -> str:
        return gemini_model()

    def call_raw(
        self,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
        timeout: int,
    ) -> LLMResponse:
        # Resolved once so every return path below names the model actually
        # asked for, including the ones that return before the HTTP call.
        model = effective_model("gemini", gemini_model())

        # ── STEP 1: Circuit breaker check — MUST be first ─────────────────────
        breaker = get_breaker("gemini")
        if not breaker.is_available():
            return LLMResponse(
                text="",
                provider="gemini",
                model=model,
                error="Circuit OPEN for Gemini",
            )

        # ── STEP 2: API key check ─────────────────────────────────────────────
        api_key = os.environ.get("GEMINI_API_KEY", "") or GEMINI_API_KEY
        if not api_key:
            return LLMResponse(
                text="",
                provider="gemini",
                model=model,
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
        # The key goes in a header, not the query string. As `?key=...` it
        # ended up in every exception message `requests` raises -- those quote
        # the URL -- and that message was assigned to LLMResponse.error and
        # logged by the router, so a single connection error wrote the API key
        # into the deployment's logs in plaintext. The API accepts either.
        url = _gemini_url(model)
        headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}

        try:
            r = http_requests.post(
                url,
                json=body,
                headers=headers,
                timeout=timeout,
            )

            if r.status_code == 429:
                # This never read Retry-After at all: it recorded a breaker
                # failure and reported a fabricated 60 seconds whatever the
                # provider had actually said. Same root cause as the primary
                # provider, in the fallback that exists to cover it.
                retry_after, pause = throttle_pause(r, default=60)
                if pause:
                    log.info(f"gemini.throttled waiting={pause:g}s then retrying once")
                    time.sleep(pause)
                    r = http_requests.post(
                        url,
                        json=body,
                        headers=headers,
                        timeout=timeout,
                    )

                if r.status_code == 429:
                    retry_after, _ = throttle_pause(r, default=retry_after)
                    breaker.record_failure(f"rate_limit retry_after={retry_after}s")
                    return LLMResponse(
                        text="",
                        provider="gemini",
                        model=model,
                        error=f"RATE_LIMIT:{retry_after}",
                    )

            # 400/401/403/404 are configuration, not an outage: an invalid
            # key, or a model this account cannot use. Google answers an
            # invalid key with 400 and API_KEY_INVALID rather than 401, so the
            # status alone is not enough — client_error_detail reads the body.
            # These used to record a breaker failure, which opened the circuit
            # on a fault that cannot recover and reported it as a provider
            # outage. The breaker is left alone now.
            if r.status_code in (400, 401, 403, 404):
                # Same repair as the primary: a model the provider no longer
                # serves is a question the provider itself can answer.
                replacement = substituted_model(
                    "gemini", "gemini", "speed", model, r, r.status_code
                )
                if replacement:
                    model = replacement
                    url = _gemini_url(model)
                    r = http_requests.post(url, json=body, headers=headers, timeout=timeout)

            if r.status_code in (400, 401, 403, 404):
                detail = client_error_detail(
                    r, r.status_code, model, "GEMINI_API_KEY", GEMINI_MODEL_ENV
                )
                log.error(f"gemini.configuration_error status={r.status_code} model={model}")
                return LLMResponse(
                    text="",
                    provider="gemini",
                    model=model,
                    error=detail,
                )

            if r.status_code >= 500:
                breaker.record_failure(f"server_error_{r.status_code}")
                return LLMResponse(
                    text="",
                    provider="gemini",
                    model=model,
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
                    model=model,
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
                model=model,
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
                model=model,
                error="Request timed out",
            )
        except Exception as e:
            # Redacted rather than trusted: this string is logged and returned,
            # and a redirect or a future change could put a credential back
            # into it. Cheap here, a rotated key if it is missing.
            safe = redact_secrets(str(e))
            breaker.record_failure(safe[:60])
            return LLMResponse(
                text="",
                provider="gemini",
                model=model,
                error=safe[:200],
            )

    def _track(self, total_tokens: int):
        """
        FIXED: tokens key now uses incrby(total_tokens) instead of incr(1) --
        same V4 bug already fixed in groq.py's _track() but missed here.
        The old code added 1 to the token counter per call regardless of how
        many tokens were consumed, making /budget data meaningless for Gemini.
        """
        try:
            import datetime
            from app.core.redis_client import get_redis

            if total_tokens <= 0:
                return
            r = get_redis()
            today = datetime.date.today().isoformat()

            tok_key = f"llm:tokens:gemini:{today}"
            r.incrby(tok_key, total_tokens)
            r.expire(tok_key, 86400)

            req_key = f"llm:requests:gemini:{today}"
            r.incr(req_key)
            r.expire(req_key, 86400)
        except Exception as e:
            log.debug(f"gemini.track_usage_failed: {e}")
