"""
app/ai/providers/base.py
V4 Sprint 2: Abstract base class for all LLM providers.

To add a new provider (e.g. Claude, GPT-4):
  1. Create app/ai/providers/claude.py
  2. Subclass LLMProvider
  3. Implement call_raw(), provider_key, model_name
  4. Register in app/ai/router.py

That's it. Zero changes to handlers or commands.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
import time


@dataclass
class LLMResponse:
    """Structured response from any LLM provider."""

    text: str  # Raw text output
    provider: str  # "groq" | "gemini" | "openrouter"
    model: str  # e.g. "llama-3.3-70b-versatile"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0  # Wall clock time
    cost_usd: float = 0.0  # Estimated cost
    used_fallback: bool = False  # Was a fallback model used?
    error: str = ""  # Non-empty if call failed


class LLMProvider(ABC):
    """
    Abstract base for all LLM providers.
    Every provider must implement call_raw().
    Router calls ask() and ask_text() — those are implemented here.
    """

    @property
    @abstractmethod
    def provider_key(self) -> str:
        """Unique key: "groq_70b", "groq_8b", "gemini", "openrouter"."""
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Full model name passed to API."""
        ...

    @abstractmethod
    def call_raw(
        self,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
        timeout: int,
    ) -> LLMResponse:
        """
        Make one API call. Returns LLMResponse.
        Raises: Never. Returns LLMResponse with error set on failure.
        """
        ...

    # ── Shared helpers (same for all providers) ───────────────────────────────

    def ask(
        self,
        system: str,
        user: str,
        max_tokens: int = 1500,
        temperature: float = 0.2,
        timeout: int = 45,
    ) -> tuple[dict, LLMResponse]:
        """
        Call provider → parse JSON → return (parsed_dict, response_meta).
        On parse failure → returns ({"raw": text}, meta).
        """
        start = time.time()
        resp = self.call_raw(system, user, max_tokens, temperature, timeout)
        resp.latency_ms = int((time.time() - start) * 1000)

        if resp.error:
            return {"error": resp.error}, resp

        parsed = _extract_json(resp.text)
        return parsed, resp

    def ask_text(
        self,
        system: str,
        user: str,
        max_tokens: int = 800,
        timeout: int = 30,
    ) -> tuple[str, LLMResponse]:
        """
        Call provider → return (plain_text, response_meta).
        """
        start = time.time()
        resp = self.call_raw(system, user, max_tokens, 0.3, timeout)
        resp.latency_ms = int((time.time() - start) * 1000)

        if resp.error:
            return "", resp

        return resp.text.strip(), resp


def _extract_json(text: str) -> dict:
    """
    Shared JSON extractor — used by all providers.
    Brace-depth method: finds first complete {...} object.
    """
    import json

    stripped = text.strip()

    # Direct parse
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Strip markdown fences
    if "```" in stripped:
        import re

        stripped = re.sub(r"```(?:json)?\n?", "", stripped).strip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

    # Brace-depth scan
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

    return {"raw": text}
