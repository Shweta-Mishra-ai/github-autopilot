"""
app/ai/circuit_breaker.py
Thread-safe circuit breaker with RLock.

"""

import time
import logging
import threading
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
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    @property
    def state(self) -> CBState:
        with self._lock:
            if (
                self._state == CBState.OPEN
                and time.time() - self._opened_at >= self.recovery_timeout
            ):
                log.info(f"circuit_breaker.half_open provider={self.provider}")
                self._state = CBState.HALF_OPEN
            return self._state

    def is_available(self) -> bool:
        return self.state in (CBState.CLOSED, CBState.HALF_OPEN)

    def record_success(self):
        with self._lock:
            if self._state != CBState.CLOSED:
                log.info(f"circuit_breaker.recovered provider={self.provider}")
            self._failures = 0
            self._state = CBState.CLOSED
            self._last_failure_reason = ""

    def record_failure(self, reason: str = ""):
        with self._lock:
            self._failures += 1
            self._last_failure_reason = reason
            if self._failures >= self.fail_threshold or self._state == CBState.HALF_OPEN:
                self._state = CBState.OPEN
                self._opened_at = time.time()
                log.warning(
                    f"circuit_breaker.opened provider={self.provider} "
                    f"failures={self._failures} reason={reason}"
                )
        # Outside the lock: health_check does its own Redis I/O, and holding
        # this RLock across it would serialise every provider's error path.
        #
        # Hooked here rather than in each provider because all four already
        # funnel failures through this method — health_check's error rate was
        # otherwise fed by nothing and always read 0%.
        try:
            from app.core.health_check import record_latency

            record_latency(self.provider, 0, is_error=True)
        except Exception:
            pass  # health tracking must never affect the breaker

    def seconds_until_retry(self) -> int:
        with self._lock:
            if self._state == CBState.OPEN:
                remaining = self.recovery_timeout - (time.time() - self._opened_at)
                return max(0, int(remaining))
            return 0

    def status(self) -> dict:
        with self._lock:
            return {
                "provider": self.provider,
                "state": self._state.value,
                "failures": self._failures,
                "last_failure": self._last_failure_reason,
                "recovers_in_seconds": self.seconds_until_retry(),
            }


# ── Module-level singletons ───────────────────────────────────────────────────

# Thresholds live here, once. They used to be inline in the dict below, which
# meant a caller that cleared the registry got breakers rebuilt by get_breaker()
# with CircuitBreaker's *default* thresholds instead of these — a quieter
# version of the same bug reset_breakers() exists to prevent.
_BREAKER_SPECS: dict[str, tuple[int, int]] = {
    # provider: (fail_threshold, recovery_timeout)
    "groq_70b": (3, 60),
    "groq_8b": (5, 30),
    "gemini": (3, 90),
    "openrouter": (5, 120),
}


def _build_registry() -> dict[str, CircuitBreaker]:
    return {
        name: CircuitBreaker(name, fail_threshold=fails, recovery_timeout=recovery)
        for name, (fails, recovery) in _BREAKER_SPECS.items()
    }


_breakers: dict[str, CircuitBreaker] = _build_registry()


def reset_breakers() -> None:
    """Restore the registry to its canonical state, all closed. Tests only.

    Breaker state is process-wide and has no expiry short of recovery_timeout,
    so a test that drives a provider to its failure threshold leaves it open for
    everything that runs afterwards. That is invisible under a fixed test order
    and turns into a confusing failure under a shuffled one: the symptom is
    "All LLM providers are unavailable" in a test that never touches a provider,
    which reads as an infrastructure fault rather than leaked state.

    Mutates the existing dict rather than rebinding the name, because modules
    that imported `_breakers` hold a reference to that object.
    """
    _breakers.clear()
    _breakers.update(_build_registry())


def get_breaker(provider: str) -> CircuitBreaker:
    """
    Always returns from _breakers dict.
    If provider unknown, adds it. Never creates a throwaway instance.
    """
    if provider not in _breakers:
        fails, recovery = _BREAKER_SPECS.get(provider, (5, 60))
        _breakers[provider] = CircuitBreaker(
            provider, fail_threshold=fails, recovery_timeout=recovery
        )
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
    def __init__(self, retry_in_seconds: int = None):
        if retry_in_seconds is not None:
            self.retry_in_seconds = int(retry_in_seconds)
        else:
            try:
                values = [int(cb.seconds_until_retry()) for cb in _breakers.values()]
                self.retry_in_seconds = max(60, min(values)) if values else 60
            except Exception:
                self.retry_in_seconds = 60
        super().__init__(f"All LLM providers are unavailable. Retry in ~{self.retry_in_seconds}s.")
