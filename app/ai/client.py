"""
AI Client - app/ai/client.py
V4: Multi-model Groq client with circuit breaker integration.

Brace-depth JSON extraction replaces greedy regex.
groq_text uses 70B model by default now.
"""

import json
import logging
import os
import time

import requests

from app.ai.circuit_breaker import AllProvidersDown, available_providers, get_breaker

log = logging.getLogger(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

MODEL_70B = "llama-3.3-70b-versatile"
MODEL_8B = "llama-3.1-8b-instant"
MAX_RETRIES = 2


class AIError(Exception):
    pass


def _call_groq(
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    temperature: float,
    timeout: int,
) -> str:
    """Single Groq API call. Returns raw text. Raises AIError on failure."""
    provider_key = "groq_70b" if ("70b" in model or "versatile" in model) else "groq_8b"
    breaker = get_breaker(provider_key)

    if not breaker.is_available():
        raise AIError(f"Circuit OPEN for {model}")

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }

    try:
        r = requests.post(GROQ_URL, headers=headers, json=payload, timeout=timeout)

        if r.status_code == 429:
            retry_after = int(r.headers.get("Retry-After", 30))
            breaker.record_failure(f"rate_limit_429 retry_after={retry_after}s")
            raise AIError(f"RATE_LIMIT:{retry_after}")

        if r.status_code >= 500:
            breaker.record_failure(f"server_error_{r.status_code}")
            raise AIError(f"Groq server error {r.status_code}")

        r.raise_for_status()

        data = r.json()
        result = data["choices"][0]["message"]["content"]

        breaker.record_success()
        _track_usage(provider_key, data.get("usage", {}))
        return result

    except requests.exceptions.Timeout:
        breaker.record_failure("timeout")
        raise AIError("Request timed out")
    except AIError:
        raise
    except Exception as e:
        breaker.record_failure(str(e)[:60])
        raise AIError(str(e))


def _track_usage(provider_key: str, usage: dict):
    """Track token usage in Redis for /budget command."""
    try:
        import datetime
        from app.core.redis_client import get_redis

        total = usage.get("total_tokens", 0)
        if total <= 0:
            return

        r = get_redis()
        today = datetime.date.today().isoformat()

        for key in (
            f"llm:tokens:{provider_key}:{today}",
            f"llm:requests:{provider_key}:{today}",
        ):
            r.incr(key)
            r.expire(key, 86400)

    except Exception:
        pass


def _extract_json(text: str) -> dict:
    """
    Brace-depth JSON extraction.
    Finds the first complete JSON object by counting opening and closing braces.
    More reliable than a greedy regex when the response contains multiple objects.
    """
    # Step 1: Direct parse (clean single-object response)
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Step 2: Strip markdown fences if present
    if "```" in stripped:
        import re

        stripped = re.sub(r"```(?:json)?\n?", "", stripped).strip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

    # Step 3: Brace-depth scan
    for start_idx, ch in enumerate(text):
        if ch != "{":
            continue
        depth = 0
        for end_idx in range(start_idx, len(text)):
            c = text[end_idx]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            if depth == 0:
                candidate = text[start_idx : end_idx + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    break

    log.warning(f"ai.json_extract_failed preview={text[:120]!r}")
    return {"raw": text}


def groq_ask(
    system: str,
    user: str,
    max_tokens: int = 1500,
    fast: bool = False,
    temperature: float = 0.2,
    timeout: int = 45,
) -> dict:
    """
    Call Groq, return parsed JSON dict.
    fast=False: try 70B first, fall back to 8B.
    fast=True:  use 8B only (for simple/cheap tasks).
    Raises AllProvidersDown when all circuits are OPEN.
    """
    if not available_providers():
        raise AllProvidersDown()

    models = [MODEL_8B] if fast else [MODEL_70B, MODEL_8B]

    for model in models:
        for attempt in range(MAX_RETRIES):
            try:
                text = _call_groq(model, system, user, max_tokens, temperature, timeout)
                return _extract_json(text)

            except AIError as e:
                msg = str(e)
                if "RATE_LIMIT:" in msg:
                    wait = int(msg.split(":")[1])
                    log.warning(f"groq_ask.rate_limit model={model} wait={wait}s")
                    time.sleep(min(wait, 30))
                    break
                log.warning(f"groq_ask.error model={model} attempt={attempt + 1}: {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2**attempt)

            except json.JSONDecodeError:
                log.warning(f"groq_ask.json_error model={model}")
                return {"raw": ""}

            except Exception as e:
                log.warning(
                    f"groq_ask.unexpected model={model} attempt={attempt + 1}: {e}"
                )
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2**attempt)

    if not available_providers():
        raise AllProvidersDown()

    log.error("groq_ask.all_models_failed")
    return {"error": "AI temporarily unavailable"}


def groq_text(
    system: str,
    user: str,
    max_tokens: int = 800,
    timeout: int = 30,
    fast: bool = False,
) -> str:
    """
    Call Groq, return plain text.
    fast=False: 70B (default, better quality for summaries/changelogs).
    fast=True:  8B (cheaper, for quick tasks).
    """
    models = [MODEL_8B] if fast else [MODEL_70B, MODEL_8B]

    for model in models:
        for attempt in range(MAX_RETRIES):
            try:
                return _call_groq(model, system, user, max_tokens, 0.3, timeout)

            except AIError as e:
                if "RATE_LIMIT" in str(e):
                    time.sleep(15)
                    break
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2**attempt)

            except Exception as e:
                log.warning(f"groq_text attempt={attempt + 1} model={model}: {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2**attempt)

    if not available_providers():
        raise AllProvidersDown()

    return "AI temporarily unavailable. Please try again in a moment."
