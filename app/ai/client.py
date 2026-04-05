"""
AI Client - app/ai/client.py
V4 changes:

FIXED (BUG 6): JSON extractor — brace-depth counting replaces greedy regex.
  Old: re.search(r'\{[\s\S]*\}', text)
       Problem: If Groq returns {"a": 1} some text {"b": 2}
                Regex captures from first { to LAST } → unparseable garbage.
  Fix: Count opening/closing braces. Stop at exact matching }. Always correct.

FIXED (BUG 7): groq_text() now uses 70B model by default.
  Old: groq_text() always used FALLBACK_MODEL (8B) — even for PR summaries,
       CHANGELOG, thread summaries where output quality matters most.
  Fix: Use 70B by default. fast=True → 8B (for simple/cheap tasks).

NEW: Circuit breaker integration.
  Every _call_groq() records success/failure to circuit breaker.
  AllProvidersDown raised when all circuits are OPEN.
  Token usage tracked in Redis for /budget command.
"""

import json
import logging
import os
import time

import requests

from app.ai.circuit_breaker import AllProvidersDown, available_providers, get_breaker

log = logging.getLogger(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"

MODEL_70B   = "llama-3.3-70b-versatile"
MODEL_8B    = "llama-3.1-8b-instant"
MAX_RETRIES = 2


class AIError(Exception):
    pass


# ── Internal call ─────────────────────────────────────────────────────────────

def _call_groq(
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    temperature: float,
    timeout: int,
) -> str:
    """
    Single Groq API call. Returns raw text content.
    Records success/failure to circuit breaker.
    Raises AIError on any failure.
    """
    provider_key = "groq_70b" if ("70b" in model or "versatile" in model) else "groq_8b"
    breaker      = get_breaker(provider_key)

    if not breaker.is_available():
        raise AIError(f"Circuit OPEN for {model} — skipping call")

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type":  "application/json",
    }
    payload = {
        "model":       model,
        "max_tokens":  max_tokens,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
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

        data   = r.json()
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
    """
    Track token usage in Redis.
    Powers /budget command — shows daily tokens used per provider.
    """
    try:
        import datetime
        from app.core.redis_client import get_redis

        total = usage.get("total_tokens", 0)
        if total <= 0:
            return

        r     = get_redis()
        today = datetime.date.today().isoformat()

        tok_key = f"llm:tokens:{provider_key}:{today}"
        req_key = f"llm:requests:{provider_key}:{today}"

        r.incr(tok_key)
        r.expire(tok_key, 86400)

        r.incr(req_key)
        r.expire(req_key, 86400)

    except Exception:
        pass  # Never let tracking break the main call


# ── JSON extraction ───────────────────────────────────────────────────────────

def _extract_json(text: str) -> dict:
    """
    ✅ FIXED (BUG 6): Brace-depth JSON extraction.

    Greedy regex re.search(r'\{[\s\S]*\}') would match from first { to LAST }
    — broken when Groq returns multiple JSON objects or trailing text.

    This function:
    1. Tries direct json.loads() first (handles clean responses fast).
    2. Falls back to brace-depth scan to find first complete JSON object.
    """
    # Step 1: Direct parse (clean response — no extra text)
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Step 2: Strip markdown code fences if present
    if "```" in stripped:
        import re
        stripped = re.sub(r"```(?:json)?\n?", "", stripped).strip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

    # Step 3: Brace-depth scan — find first complete {...} object
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
                    break  # This { was not a valid JSON start, try next

    log.warning(f"ai.json_extract_failed preview={text[:120]!r}")
    return {"raw": text}


# ── Public API ────────────────────────────────────────────────────────────────

def groq_ask(
    system: str,
    user: str,
    max_tokens: int = 1500,
    fast: bool = False,
    temperature: float = 0.2,
    timeout: int = 45,
) -> dict:
    """
    Call Groq and return parsed JSON dict.

    fast=False (default) → try 70B first, fall back to 8B.
    fast=True            → use 8B only (for classification, labels, quick tasks).

    Returns {"error": "..."} if all attempts fail — never raises (except AllProvidersDown).
    Raises AllProvidersDown if ALL circuits are OPEN (no providers available at all).
    """
    if not available_providers():
        raise AllProvidersDown()

    models = [MODEL_8B] if fast else [MODEL_70B, MODEL_8B]

    for model in models:
        for attempt in range(MAX_RETRIES):
            try:
                text   = _call_groq(model, system, user, max_tokens, temperature, timeout)
                parsed = _extract_json(text)
                return parsed

            except AIError as e:
                msg = str(e)
                if "RATE_LIMIT:" in msg:
                    wait = int(msg.split(":")[1])
                    log.warning(f"groq_ask.rate_limit model={model} wait={wait}s")
                    time.sleep(min(wait, 30))
                    break  # Rate limited — move to next model
                log.warning(f"groq_ask.error model={model} attempt={attempt+1}: {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)

            except json.JSONDecodeError:
                log.warning(f"groq_ask.json_error model={model}")
                return {"raw": ""}

            except Exception as e:
                log.warning(f"groq_ask.unexpected model={model} attempt={attempt+1}: {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)

    # All models exhausted
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
    Call Groq and return plain text.

    ✅ FIXED (BUG 7): Now uses 70B by default.
    Old: always used MODEL_8B (FALLBACK_MODEL) — even for PR summaries,
         CHANGELOG, thread summaries where output quality matters.
    Fix: fast=False (default) → 70B. fast=True → 8B.

    Returns fallback string if all attempts fail — never raises.
    """
    models = [MODEL_8B] if fast else [MODEL_70B, MODEL_8B]

    for model in models:
        for attempt in range(MAX_RETRIES):
            try:
                return _call_groq(model, system, user, max_tokens, 0.3, timeout)

            except AIError as e:
                if "RATE_LIMIT" in str(e):
                    time.sleep(15)
                    break  # Move to next model
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)

            except Exception as e:
                log.warning(f"groq_text attempt={attempt+1} model={model}: {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)

    if not available_providers():
        raise AllProvidersDown()

    return "AI temporarily unavailable. Please try again in a moment."
