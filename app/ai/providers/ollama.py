"""
app/ai/providers/ollama.py — Local LLM provider (Ollama).

WHY THIS EXISTS
  Groq/Gemini/OpenRouter are third parties: every /fix ships your source code
  off-box. For private or regulated repos that is unacceptable data egress.
  Ollama runs the model on hardware you control, so code never leaves your
  infrastructure. Enable it and (optionally) pin the router to local-only mode
  so NO cloud provider is ever contacted — see app/ai/router.py.

CONFIG (env)
  OLLAMA_HOST   Base URL of the Ollama server, e.g. http://localhost:11434
                (unset → provider is inactive; router skips it).
  OLLAMA_MODEL  Model tag, default "llama3.1:8b".

PRIVACY GUARANTEE
  cost_usd is always 0 and no usage is reported to any external service. The
  only network call is to OLLAMA_HOST, which you own.
"""

import logging
import os

import requests as http_requests

import app.ai.circuit_breaker as cb
from app.ai.providers.base import LLMProvider, LLMResponse

log = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")


def is_configured() -> bool:
    """True if an Ollama host is configured — router uses this to skip it cleanly."""
    return bool(os.environ.get("OLLAMA_HOST", "").strip())


class OllamaProvider(LLMProvider):
    def __init__(self, model: str = ""):
        self._model = model or DEFAULT_MODEL

    @property
    def provider_key(self) -> str:
        return "ollama"

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
        breaker = cb.get_breaker(self.provider_key)
        if not breaker.is_available():
            return LLMResponse(
                text="",
                provider="ollama",
                model=self._model,
                error=f"Circuit OPEN for {self._model}",
            )

        host = os.environ.get("OLLAMA_HOST", "").strip().rstrip("/")
        if not host:
            return LLMResponse(
                text="",
                provider="ollama",
                model=self._model,
                error="OLLAMA_HOST not set",
            )

        body = {
            "model": self._model,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }

        try:
            # Local inference on CPU can be slow — give it more headroom.
            r = http_requests.post(f"{host}/api/chat", json=body, timeout=max(timeout, 120))

            try:
                status = int(r.status_code)
            except (TypeError, ValueError):
                status = 0
            if status >= 500:
                breaker.record_failure(f"server_error_{status}")
                return LLMResponse(
                    text="",
                    provider="ollama",
                    model=self._model,
                    error=f"Server error {status}",
                )

            r.raise_for_status()
            data = r.json()

            # Ollama /api/chat → {"message": {"content": "..."}, "prompt_eval_count", "eval_count"}
            text = (data.get("message") or {}).get("content", "")
            p_tok = int(data.get("prompt_eval_count", 0) or 0)
            c_tok = int(data.get("eval_count", 0) or 0)

            breaker.record_success()
            return LLMResponse(
                text=text,
                provider="ollama",
                model=self._model,
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                total_tokens=p_tok + c_tok,
                cost_usd=0.0,  # local — always free, always private
            )

        except http_requests.exceptions.Timeout:
            breaker.record_failure("timeout")
            return LLMResponse(
                text="",
                provider="ollama",
                model=self._model,
                error="Local inference timed out — model too large or host overloaded",
            )
        except http_requests.exceptions.ConnectionError:
            breaker.record_failure("connection")
            return LLMResponse(
                text="",
                provider="ollama",
                model=self._model,
                error=f"Cannot reach Ollama at {host} — is it running?",
            )
        except Exception as e:
            breaker.record_failure(str(e)[:60])
            return LLMResponse(
                text="",
                provider="ollama",
                model=self._model,
                error=str(e)[:200],
            )
