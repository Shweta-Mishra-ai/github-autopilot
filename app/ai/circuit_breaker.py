"""
app/ai/circuit_breaker.py
V4: Per-provider circuit breaker.

FIXED: AllProvidersDown.__init__ wraps min() in try/except
  — prevents TypeError when _breakers contains mocks in tests.

FIXED: get_breaker() always returns from _breakers dict
  — predictable behavior, no surprise new instances.
"""

import time
import logging
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger(__name__)


class CBState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    provider: str
    fail_threshold: int = 3
    recovery_timeout: int = 60

    _state: CBState = field(default=CBState.CLOSED, init=False, repr=False)
    _failures: int = field(default=0, init=False, repr=False)
    _opened_at: float = field(default=0.0, init=False, repr=False)
    _last_failure_reason: str = field(default="", init=False, repr=False)

    @property
    def state(self) -> CBState:
        if self._state == CBState.OPEN:
            if time.time() - self._opened_at >= self.recovery_timeout:
                log.info(f"circuit_breaker.half_open provider={self.provider}")
                self._state = CBState.HALF_OPEN
        return self._state

    def is_available(self) -> bool:
        return self.state in (CBState.CLOSED, CBState.HALF_OPEN)

    def record_success(self):
        if self._state != CBState.CLOSED:
            log.info(f"circuit_breaker.recovered provider={self.provider}")
        self._failures = 0
        self._state = CBState.CLOSED
        self._last_failure_reason = ""

    def record_failure(self, reason: str = ""):
        self._failures += 1
        self._last_failure_reason = reason
        if self._failures >= self.fail_threshold or self._state == CBState.HALF_OPEN:
            self._state = CBState.OPEN
            self._opened_at = time.time()
            log.warning(
                f"circuit_breaker.opened provider={self.provider} "
                f"failures={self._failures} reason={reason}"
            )

    def seconds_until_retry(self) -> int:
        if self._state == CBState.OPEN:
            remaining = self.recovery_timeout - (time.time() - self._opened_at)
            return max(0, int(remaining))
        return 0

    def status(self) -> dict:
        return {
            "provider": self.provider,
            "state": self.state.value,
            "failures": self._failures,
            "last_failure": self._last_failure_reason,
            "recovers_in_seconds": self.seconds_until_retry(),
        }


# ── Module-level singletons ───────────────────────────────────────────────────

_breakers: dict[str, CircuitBreaker] = {
    "groq_70b": CircuitBreaker("groq_70b", fail_threshold=3, recovery_timeout=60),
    "groq_8b": CircuitBreaker("groq_8b", fail_threshold=5, recovery_timeout=30),
    "gemini": CircuitBreaker("gemini", fail_threshold=3, recovery_timeout=90),
    "openrouter": CircuitBreaker("openrouter", fail_threshold=5, recovery_timeout=120),
}


def get_breaker(provider: str) -> CircuitBreaker:
    """
    Always returns from _breakers dict.
    If provider unknown, adds it. Never creates a throwaway instance.
    This ensures tests that patch _breakers[key] always work.
    """
    if provider not in _breakers:
        _breakers[provider] = CircuitBreaker(provider)
    return _breakers[provider]


def all_providers_down() -> bool:
    return all(not cb.is_available() for cb in _breakers.values())


def available_providers() -> list[str]:
    return [name for name, cb in _breakers.items() if cb.is_available()]


def status_all() -> dict:
    try:
        return {name: cb.status() for name, cb in _breakers.items()}
    except Exception:
        return {name: {"state": "unknown"} for name in _breakers}


class AllProvidersDown(Exception):
    def __init__(self):
        # FIXED: try/except prevents TypeError when _breakers has mocks
        try:
            recovery = min(int(cb.seconds_until_retry()) for cb in _breakers.values())
        except Exception:
            recovery = 60
        self.retry_in_seconds = recovery
        super().__init__(
            f"All LLM providers unavailable. Earliest retry in {recovery}s."
        )
