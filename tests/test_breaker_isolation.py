"""
tests/test_breaker_isolation.py

Circuit-breaker state is process-wide, and a test that opens one used to leave
it open for every test that ran afterwards.

This cost a CI run on main. tests/test_router.py::test_ask_calls_provider failed
with "All LLM providers are unavailable" under seed 594930018 — a test that
patches _call_provider and never touches a real provider. Probing the router at
the point of failure showed usage at 0% and both Groq breakers open: the test
had inherited the state, not created it.

An infrastructure-shaped message produced by leaked state is the worst kind of
failure to read, because it sends whoever sees it to look at the provider.

The guard is an autouse fixture in conftest. These tests check the guard, since
a guard nobody tests can be removed without anyone noticing — which is exactly
what happened to the "random ordering" that was supposed to catch this.
"""

from __future__ import annotations

import pytest

from app.ai.circuit_breaker import (
    _BREAKER_SPECS,
    _breakers,
    get_breaker,
    reset_breakers,
)

PROVIDERS = sorted(_BREAKER_SPECS)


def _trip(name: str) -> None:
    """Drive a breaker past its threshold."""
    breaker = get_breaker(name)
    for _ in range(_BREAKER_SPECS[name][0] + 2):
        breaker.record_failure()


class TestTheFixtureGivesEveryTestAClosedCircuit:
    """These two run in either order. Whichever goes first leaves a tripped
    breaker behind; the other asserts it did not inherit it."""

    def test_a_opens_every_breaker(self):
        for name in PROVIDERS:
            _trip(name)
        assert not get_breaker("groq_70b").is_available()

    def test_b_starts_with_every_breaker_closed(self):
        for name in PROVIDERS:
            assert get_breaker(name).is_available(), (
                f"{name} arrived open — breaker state is leaking between tests"
            )


class TestResetBreakers:
    def test_it_closes_an_open_breaker(self):
        _trip("groq_70b")
        assert not get_breaker("groq_70b").is_available()

        reset_breakers()
        assert get_breaker("groq_70b").is_available()

    def test_it_restores_every_provider(self):
        for name in PROVIDERS:
            _trip(name)
        reset_breakers()
        assert all(get_breaker(n).is_available() for n in PROVIDERS)

    def test_it_repopulates_a_cleared_registry(self):
        """Two test files clear the dict outright rather than resetting it."""
        _breakers.clear()
        reset_breakers()
        assert set(_breakers) == set(PROVIDERS)

    def test_it_mutates_the_dict_rather_than_rebinding_it(self):
        """Modules that did `from app.ai.circuit_breaker import _breakers` hold
        a reference to that object. Rebinding the name would leave them looking
        at a registry nothing else updates."""
        before = id(_breakers)
        reset_breakers()
        assert id(_breakers) == before


class TestThresholdsSurviveAReset:
    """get_breaker() used to rebuild a missing provider with CircuitBreaker's
    default thresholds, so a cleared registry came back with groq_70b tolerating
    five failures instead of three. Quieter than the leak, same shape."""

    @pytest.mark.parametrize("name", PROVIDERS)
    def test_a_rebuilt_breaker_keeps_its_configured_threshold(self, name):
        expected_fails, expected_recovery = _BREAKER_SPECS[name]

        _breakers.clear()
        rebuilt = get_breaker(name)

        assert rebuilt.fail_threshold == expected_fails
        assert rebuilt.recovery_timeout == expected_recovery

    @pytest.mark.parametrize("name", PROVIDERS)
    def test_reset_rebuilds_with_the_configured_threshold(self, name):
        expected_fails, expected_recovery = _BREAKER_SPECS[name]

        reset_breakers()
        breaker = get_breaker(name)

        assert breaker.fail_threshold == expected_fails
        assert breaker.recovery_timeout == expected_recovery

    def test_an_unknown_provider_still_gets_a_breaker(self):
        breaker = get_breaker("some_future_provider")
        assert breaker is not None
        assert breaker.is_available()


class TestTheRouterIsNotAtTheMercyOfEarlierTests:
    def test_ask_works_with_a_patched_provider_after_breakers_were_tripped(self):
        """The exact failure from the CI run, as a test.

        ask() calls _select_provider before _call_provider, so patching only the
        latter leaves the selection at the mercy of whatever breaker state the
        session happens to be carrying.
        """
        from unittest.mock import patch

        from app.ai.providers.base import LLMResponse
        from app.ai.router import LLMRouter

        for name in PROVIDERS:
            _trip(name)
        reset_breakers()  # what the autouse fixture does for every test

        router = LLMRouter()
        response = LLMResponse(
            text="answer", provider="groq", model="m", total_tokens=10, latency_ms=5
        )
        with patch.object(router, "_call_provider", return_value=response):
            _result, meta = router.ask("system", "user", task="test")

        assert meta is not None
