"""
tests/test_quota_end_to_end.py

A spent provider quota, followed from the HTTP 429 to the reply a user reads.

Each link had its own test — the provider passes Retry-After to the breaker
(with the breaker mocked), the breaker holds for it (built by hand), the reply
says "Quota Reached" (given a payload) — and the chain still did not work on
the one deployment that matters: Groq configured, Gemini and OpenRouter not.

- AllProvidersDown took the earliest retry over every breaker, and the
  breakers of providers with no key are closed with 0 to wait. A quota hours
  from resetting was reported as "retry in ~60s".
- parse_retry_after clamps to an hour (sized for the GitHub client, which
  sleeps for it), so a 4-hour quota held the breaker for one and the reply
  said "resets in ~59 min".

Only the HTTP layer is faked here.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.ai import circuit_breaker as cb
from app.ai import guarded
from app.ai.providers import groq as groq_mod

FOUR_HOURS = 4 * 3600


@pytest.fixture
def groq_only(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_value")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("LLM_LOCAL_ONLY", raising=False)
    monkeypatch.delenv("LLM_PREFER_LOCAL", raising=False)
    cb.reset_breakers()
    yield
    cb.reset_breakers()


def _quota_spent(retry_after: str):
    def post(*_a, **_k):
        r = MagicMock()
        r.status_code = 429
        r.headers = {"Retry-After": retry_after}
        r.content = b"{}"
        r.text = "{}"
        return r

    return post


def _ask_until_down(router):
    """Ask as users would until the breakers give up; return the last payload."""
    payload = {}
    with patch.object(groq_mod.http_requests, "post", side_effect=_quota_spent(str(FOUR_HOURS))), \
         patch("app.ai.router.router", router), \
         patch.object(guarded, "_alert_providers_down"):
        for _ in range(4):
            payload, _verdict = guarded.guarded_ask("s", "u", task="fix")
            if payload.get("_reason") == "providers_down":
                break
    return payload


class TestASpentGroqQuotaOnAGroqOnlyDeployment:
    def test_the_user_is_told_it_is_a_quota_and_when_it_resets(self, groq_only):
        from app.ai.router import LLMRouter

        payload = _ask_until_down(LLMRouter())

        assert payload.get("_reason") == "providers_down", payload
        assert payload["_retry_in"] > FOUR_HOURS - 60, (
            f"retry_in={payload['_retry_in']}: the wait the provider gave was lost"
        )
        out = guarded.degraded_comment(payload, "fix")
        assert "Quota Reached" in out
        assert "~3h 59m" in out or "~4h 0m" in out, out
        assert "try again shortly" not in out.lower()

    def test_the_breakers_hold_for_the_providers_wait_not_an_hour(self, groq_only):
        from app.ai.router import LLMRouter

        _ask_until_down(LLMRouter())
        held = cb.get_breaker("groq_70b")
        assert not held.is_available()
        assert held.seconds_until_retry() > FOUR_HOURS - 60

    def test_a_slash_command_says_the_same(self, groq_only):
        from app.ai.router import LLMRouter
        from app.handlers.comments.dispatcher import make_degraded_response

        payload = _ask_until_down(LLMRouter())
        out = make_degraded_response({"_providers_down": True, "_retry_in": payload["_retry_in"]})
        assert "Quota Reached" in out
        assert "in a minute" not in out


class TestTheEarliestRetryComesFromOpenBreakers:
    def test_a_provider_with_no_key_does_not_shorten_the_wait(self, groq_only):
        cb.get_breaker("groq_70b").record_rate_limit(FOUR_HOURS)
        cb.get_breaker("groq_8b").record_rate_limit(FOUR_HOURS)
        # gemini and openrouter: closed, never called
        assert cb.AllProvidersDown().retry_in_seconds > FOUR_HOURS - 60

    def test_the_earliest_open_breaker_still_wins(self, groq_only):
        cb.get_breaker("groq_70b").record_rate_limit(FOUR_HOURS)
        gemini = cb.get_breaker("gemini")
        for _ in range(gemini.fail_threshold):
            gemini.record_failure("503")
        assert cb.AllProvidersDown().retry_in_seconds <= gemini.recovery_timeout

    def test_nothing_open_falls_back_to_a_minute(self, groq_only):
        # e.g. a quality-floor refusal: no breaker is open.
        assert cb.AllProvidersDown().retry_in_seconds == 60

    def test_an_explicit_value_is_kept(self, groq_only):
        assert cb.AllProvidersDown(45).retry_in_seconds == 45


class TestHowLongAProviderWaitIsBelieved:
    def test_a_providers_wait_of_hours_is_kept(self):
        from app.ai.providers.base import throttle_pause

        r = MagicMock(headers={"Retry-After": str(FOUR_HOURS)})
        retry_after, pause = throttle_pause(r)
        assert retry_after == FOUR_HOURS
        assert pause == 0.0, "nothing may sleep for it"

    def test_an_absurd_wait_is_still_clamped_to_a_day(self):
        from app.ai.providers.base import MAX_PROVIDER_RETRY_AFTER, throttle_pause

        r = MagicMock(headers={"Retry-After": str(10 * 365 * 86400)})
        assert throttle_pause(r)[0] == MAX_PROVIDER_RETRY_AFTER == 86400

    def test_the_github_client_clamp_is_unchanged(self):
        # The client sleeps for this one, so it stays at an hour.
        from app.core.retry_after import MAX_RETRY_AFTER, parse_retry_after

        assert parse_retry_after(str(FOUR_HOURS)) == MAX_RETRY_AFTER == 3600

    def test_the_cap_also_applies_to_the_date_form(self):
        import time
        from email.utils import formatdate

        from app.core.retry_after import parse_retry_after

        when = formatdate(time.time() + FOUR_HOURS, usegmt=True)
        assert parse_retry_after(when) == 3600
        assert parse_retry_after(when, cap=86400) > FOUR_HOURS - 5
