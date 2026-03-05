"""
AI Client - app/ai/client.py
All Groq API calls go through here.
Features: retry, model fallback, timeout, structured error handling.
"""

import os
import time
import logging
import requests

log = logging.getLogger(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

PRIMARY_MODEL = "llama-3.3-70b-versatile"
FALLBACK_MODEL = "llama-3.1-8b-instant"
MAX_RETRIES = 3


class AIError(Exception):
    pass


def _call_groq(model: str, system: str, user: str,
               max_tokens: int, temperature: float, timeout: int) -> str:
    """Single Groq API call. Returns raw text content."""
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
    r = requests.post(GROQ_URL, headers=headers, json=payload, timeout=timeout)

    if r.status_code == 429:
        retry_after = int(r.headers.get("Retry-After", 30))
        raise AIError(f"RATE_LIMIT:{retry_after}")

    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def groq_ask(system: str, user: str,
             max_tokens: int = 1500,
             fast: bool = False,
             temperature: float = 0.2,
             timeout: int = 45) -> dict:
    """
    Call Groq and return parsed JSON dict.
    - Tries primary model first (70B), falls back to 8B on rate limit
    - Retries up to MAX_RETRIES times with backoff
    - Returns {"error": "..."} if all attempts fail — never raises
    """
    import re, json as _json

    models = [FALLBACK_MODEL] if fast else [PRIMARY_MODEL, FALLBACK_MODEL]

    for model in models:
        for attempt in range(MAX_RETRIES):
            try:
                text = _call_groq(model, system, user, max_tokens, temperature, timeout)

                # Extract JSON from response
                match = re.search(r'\{[\s\S]*\}', text)
                if not match:
                    log.warning(f"[{model}] No JSON in response: {text[:100]}")
                    return {"raw": text}

                parsed = _json.loads(match.group())
                return parsed

            except AIError as e:
                msg = str(e)
                if msg.startswith("RATE_LIMIT:"):
                    wait = int(msg.split(":")[1])
                    log.warning(f"[{model}] Rate limit — waiting {wait}s before fallback")
                    time.sleep(min(wait, 30))
                    break   # try next model
                log.warning(f"[{model}] attempt {attempt+1} AIError: {e}")
                time.sleep(2 ** attempt)

            except _json.JSONDecodeError as e:
                log.warning(f"[{model}] JSON parse failed: {e}")
                return {"raw": text if 'text' in dir() else ""}

            except requests.exceptions.Timeout:
                log.warning(f"[{model}] Timeout (attempt {attempt+1})")
                time.sleep(2 ** attempt)

            except Exception as e:
                log.warning(f"[{model}] Unexpected error (attempt {attempt+1}): {e}")
                time.sleep(2 ** attempt)

    log.error("All AI models failed")
    return {"error": "AI temporarily unavailable"}


def groq_text(system: str, user: str,
              max_tokens: int = 800,
              timeout: int = 30) -> str:
    """
    Call Groq and return plain text.
    Returns fallback string if all attempts fail — never raises.
    """
    for attempt in range(MAX_RETRIES):
        try:
            return _call_groq(FALLBACK_MODEL, system, user, max_tokens, 0.3, timeout)
        except AIError as e:
            if "RATE_LIMIT" in str(e):
                time.sleep(15)
            else:
                time.sleep(2 ** attempt)
        except Exception as e:
            log.warning(f"groq_text attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)

    return "AI temporarily unavailable. Please try again in a moment."
