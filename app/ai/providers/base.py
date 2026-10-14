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
import os
import re
import time


@dataclass
class LLMResponse:
    """Structured response from any LLM provider."""

    text: str  # Raw text output
    provider: str  # "groq" | "gemini" | "openrouter"
    model: str  # e.g. "openai/gpt-oss-120b"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0  # Wall clock time
    cost_usd: float = 0.0  # Estimated cost
    used_fallback: bool = False  # Was a fallback model used?
    error: str | None = None  # Non-None if call failed
    # Which configured provider answered (groq_70b, groq_8b, gemini, …), set by
    # the router. `provider` is the vendor and cannot tell the tiers apart.
    provider_key: str = ""

    @property
    def is_error(self) -> bool:
        """True if the response represents an error."""
        return self.error is not None


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

        # `or ""`: a reasoning model that spends the budget thinking returns
        # content null, and this raised AttributeError instead of an error.
        return (resp.text or "").strip(), resp


# A completion the provider stopped at max_tokens. No provider read the stop
# reason, so a cut-off answer was returned as a success: plain-text callers
# (/changelog, /explain, the PR summary) published half an answer as the
# whole one, and JSON callers saw a fragment and reported "the model returned
# garbage". The provider is healthy — its circuit is not charged — but the
# answer is not one, so it is returned as an error and another provider tried.
TRUNCATED = "TRUNCATED"
_CUT_REASONS = frozenset({"length", "max_tokens"})


def was_cut_off(finish_reason) -> bool:
    """True for every provider's spelling of "stopped at the token limit"."""
    return str(finish_reason or "").strip().lower() in _CUT_REASONS


def truncated_error(max_tokens: int) -> str:
    return f"{TRUNCATED}: the answer reached its {max_tokens}-token limit and was cut off"


class AnswerTruncated(Exception):
    """Every provider tried stopped at the token limit. Not an outage, so not
    retried: the same prompt with the same limit ends the same way."""


def _extract_json(text: str) -> dict:
    """
    The first JSON OBJECT in a model's answer, or {"raw": text}.

    Shared by all providers. The JSON decoder itself finds the object
    (raw_decode at each "{"), so braces inside strings are understood. The
    hand-written brace counter this replaces did not: with any preamble
    ("Here is the review:") a code snippet such as "if (x) {" inside a string
    unbalanced the count, and a valid answer was published as unparseable.

    Always a dict. A bare `null`, number or list parsed directly and was
    returned as-is, despite the signature; autofix's `"raw" in r` then raised
    TypeError on None.
    """
    import json
    import re

    stripped = (text or "").strip()
    decoder = json.JSONDecoder()

    candidates = [stripped]
    if "```" in stripped:
        candidates.append(re.sub(r"```(?:json)?\n?", "", stripped).strip())
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value

    for match in re.finditer(r"\{", stripped):
        try:
            value, _end = decoder.raw_decode(stripped, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value

    return {"raw": text}


# ── Permanent provider faults ────────────────────────────────────────────────
#
# A 4xx from a model provider is CONFIGURATION, not an outage, and the
# difference decides whether retrying can ever work. A retired model id, a
# rejected key and a model the account cannot reach all return 4xx and none of
# them recover on their own.
#
# Recording them as circuit-breaker failures is what hid the Groq outage: three
# requests opened the breaker on the primary, five more on the fallback, and
# the router reported "all providers down" — sending a maintainer to check
# provider status when the fix was one environment variable.
#
# A breaker exists to stop hammering a service that MIGHT recover. These will
# not, so opening it only replaces a precise error with a vague one.
CONFIG_ERROR_PREFIX = "CONFIG_ERROR:"


def is_configuration_error(error: "str | None") -> bool:
    """True when an LLMResponse.error describes a fault retrying cannot fix."""
    return bool(error) and str(error).startswith(CONFIG_ERROR_PREFIX)


def client_error_detail(
    response,
    status: int,
    model: str,
    key_env: str,
    model_env: str = "",
) -> str:
    """
    An actionable message for a 4xx that will never fix itself.

    Reads the provider's own error code where it gives one rather than
    inferring from the status alone — `model_not_found` and `API_KEY_INVALID`
    are more specific than "404" and "400", and providers disagree about which
    status they use for which fault.
    """
    code = ""
    try:
        body = response.json()
        err = body.get("error") or {}
        code = f"{err.get('code', '')} {err.get('status', '')} {err.get('message', '')}".lower()
    except Exception:
        code = ""

    where = f" Set {model_env} to a model id the provider currently serves." if model_env else ""

    if status == 402 or "insufficient" in code or "credit" in code:
        return (
            f"{CONFIG_ERROR_PREFIX} the provider refused the request for `{model}` "
            f"({status}) because the account is out of credit. Retrying will not "
            f"help until it is topped up."
        )
    if status == 404 or "model_not_found" in code or "not found" in code:
        return (
            f"{CONFIG_ERROR_PREFIX} the provider does not serve the model "
            f"`{model}`. This is configuration, not an outage — every AI command "
            f"fails until it is fixed.{where}"
        )
    if status in (401, 403) or "api_key_invalid" in code or "permission" in code:
        return (
            f"{CONFIG_ERROR_PREFIX} the provider rejected {key_env} ({status}). "
            f"The key is set but not valid, or lacks access to `{model}` — "
            f"regenerate it and update the deployment's environment."
        )
    return (
        f"{CONFIG_ERROR_PREFIX} the provider refused the request for `{model}` "
        f"({status}). The key may lack access to this model.{where}"
    )


# ── Throttling ───────────────────────────────────────────────────────────────
#
# A 429 is the provider working correctly and asking us to slow down. It is not
# the provider being unhealthy, and conflating the two is what turned a
# six-second pause into a total outage: the delay was parsed, a circuit-breaker
# failure was recorded, and the router fell back to the same provider's other
# tier — which shares the account limit — so that failed too. Three failures
# opened one breaker, five more opened the other, and the router reported "all
# providers down".
#
# Shared because both providers had it. Gemini was worse: it never read the
# header at all and reported a fabricated 60-second delay whatever the provider
# had actually said.
MAX_THROTTLE_WAIT_SECONDS = float(os.environ.get("LLM_MAX_THROTTLE_WAIT_SECONDS", "10"))


def throttle_pause(response, default: int = 30) -> tuple[int, float]:
    """
    (retry_after, seconds_to_sleep) for a 429.

    `seconds_to_sleep` is 0 when the delay is longer than we are willing to
    hold a shared worker thread for — a minute-long wait blocks other webhooks,
    so it is reported instead. `retry_after` is always the provider's own
    number, so callers report what it said rather than a guess.
    """
    from app.core.retry_after import parse_retry_after

    retry_after = parse_retry_after(_header(response, "Retry-After"), default)
    if 0 < retry_after <= MAX_THROTTLE_WAIT_SECONDS:
        return retry_after, float(retry_after)
    return retry_after, 0.0


def _header(response, name: str):
    try:
        return response.headers.get(name)
    except Exception:
        return None


# ── Never let a credential into an error string ──────────────────────────────
#
# Re-exported from core so the providers and the notification senders share
# one implementation. Both leaked a credential the same way: a URL carrying
# it, quoted back by requests in an exception, assigned to an error string
# and logged. See app/core/redaction.py.
from app.core.redaction import redact_secrets  # noqa: E402  (re-export)

__all__ = [
    "LLMProvider",
    "LLMResponse",
    "CONFIG_ERROR_PREFIX",
    "MAX_THROTTLE_WAIT_SECONDS",
    "client_error_detail",
    "is_model_missing",
    "substituted_model",
    "is_configuration_error",
    "redact_secrets",
    "throttle_pause",
]


# ── A model that no longer exists ─────────────────────────────────────────────
#
# Distinct from every other 4xx: a rejected key needs a human, but a retired
# model id has an answer the provider itself will give us. The last time one
# was retired here, every AI command returned 404 for six days.
_MODEL_GONE = re.compile(
    r"(model_not_found|does not exist|decommissioned|deprecated|no endpoints found"
    r"|not found|unknown model|invalid model)",
    re.IGNORECASE,
)


def is_model_missing(response, status: int) -> bool:
    """
    True when this 4xx says the MODEL is the problem, not the credential.

    Deliberately narrow. Substituting a model for a rejected key would swap a
    precise "your key is invalid" for a confusing "we quietly changed model and
    it still did not work", so 401/403 never reach here.
    """
    if status in (401, 403):
        return False
    body = ""
    try:
        err = (response.json().get("error") or {}) if response is not None else {}
        body = f"{err.get('code', '')} {err.get('message', '')} {err.get('status', '')}"
    except Exception:
        body = ""
    return status == 404 or bool(_MODEL_GONE.search(body))


def substituted_model(
    provider_key: str, provider: str, tier: str, failed_model: str, response, status: int
) -> str:
    """
    A replacement for a model the provider says it does not serve, or "".

    "" means "carry on and report the configuration error exactly as before",
    so this can only turn a hard failure into a working request or leave the
    behaviour unchanged. It never makes a working call worse.
    """
    if not is_model_missing(response, status):
        return ""
    try:
        from app.ai.model_catalog import substitute

        return substitute(provider_key, provider, tier, failed_model)
    except Exception:  # pragma: no cover - the repair path must never raise
        return ""
