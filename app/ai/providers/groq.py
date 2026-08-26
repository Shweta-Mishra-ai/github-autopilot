"""
app/ai/providers/groq.py
V5 — Fixed token tracking.

FIXES vs V4:
  1. TOKEN COUNTER BUG: _track() used r.incr(tokens_key) which always adds 1,
     not the actual token count. Replaced with r.incrby(tokens_key, total_tokens).
     The /budget command now shows real token usage instead of a request count.
  2. REQUEST COUNTER separated from token counter: tokens tracked via incrby,
     requests tracked via incr(1) — both correct.
"""

import logging
import os

import requests as http_requests

import app.ai.circuit_breaker as cb
from app.ai.providers.base import LLMProvider, LLMResponse, client_error_detail
from app.core.redis_client import get_redis
from app.core.retry_after import parse_retry_after

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
        breaker = cb.get_breaker(self.provider_key)
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
            r = http_requests.post(GROQ_URL, headers=headers, json=body, timeout=timeout)

            if r.status_code == 429:
                # Groq sends fractional delays (e.g. "7.66"), which int()
                # cannot parse; parse_retry_after rounds them up instead.
                retry_after = parse_retry_after(r.headers.get("Retry-After"), 30)
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

            # A 4xx here is CONFIGURATION, not an outage, and the difference
            # decides whether retrying can ever work.
            #
            # A retired model id returns 404. That used to fall through to
            # raise_for_status() and be recorded as a breaker failure, so three
            # requests opened the circuit on groq_70b, five more opened it on
            # groq_8b, and the router reported "all providers down" — sending a
            # maintainer to check provider status when the fix was one
            # environment variable. It happened: the nightly evals scored 0.0
            # and filed an issue blaming review quality.
            #
            # A breaker exists to stop hammering a service that might recover.
            # A model that does not exist, or a key that is not valid, will not
            # recover on its own, so opening the breaker only replaces a precise
            # error with a vague one. Report these and leave the breaker alone.
            if _status in (401, 403, 404):
                detail = client_error_detail(
                    r, _status, self._model, "GROQ_API_KEY", "LLM_PRIMARY_MODEL"
                )
                log.error(f"groq.configuration_error status={_status} model={self._model}")
                return LLMResponse(
                    text="",
                    provider="groq",
                    model=self._model,
                    error=detail,
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

        except http_requests.exceptions.Timeout:
            breaker.record_failure("timeout")
            return LLMResponse(
                text="",
                provider="groq",
                model=self._model,
                error="Request timed out",
            )
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
        """
        Track token + request usage in Redis for /budget command.

        FIXED: tokens key now uses incrby(total_tokens) instead of incr(1).
        The old code incremented the token counter by 1 per call regardless
        of how many tokens were consumed, making /budget data meaningless.
        """
        try:
            import datetime

            if total_tokens <= 0:
                return
            r = get_redis()
            today = datetime.date.today().isoformat()

            # Tokens: add actual count
            tok_key = f"llm:tokens:{self.provider_key}:{today}"
            r.incrby(tok_key, total_tokens)
            r.expire(tok_key, 86400)

            # Requests: always +1 per call
            req_key = f"llm:requests:{self.provider_key}:{today}"
            r.incr(req_key)
            r.expire(req_key, 86400)

        except Exception as e:
            log.debug(f"groq.track_usage_failed provider={self.provider_key}: {e}")
