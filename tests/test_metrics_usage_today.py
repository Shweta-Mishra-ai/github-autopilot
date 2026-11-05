"""get_usage_today had no test anywhere in the repository — the one real gap
in the coverage report on PR #110."""

import datetime
from unittest.mock import MagicMock, patch

from app.ai import metrics


class _FrozenDatetime(datetime.datetime):
    """23:30 on 1 March in UTC+5:30 is still 28 February in UTC."""

    @classmethod
    def now(cls, tz=None):
        if tz is None:  # the server's local clock, in UTC+5:30
            return datetime.datetime(2026, 3, 1, 23, 30)
        return datetime.datetime(2026, 2, 28, 18, 0, tzinfo=datetime.timezone.utc).astimezone(tz)


def _redis(values):
    r = MagicMock()
    r.get.side_effect = lambda key: values.get(key)
    return r


def _usage(values):
    with (
        patch.object(metrics.datetime, "datetime", _FrozenDatetime),
        patch("app.core.redis_client.get_redis", return_value=_redis(values)),
    ):
        return metrics.get_usage_today()


def test_reads_the_counters_for_today_in_utc():
    out = _usage({"llm:requests:gemini:2026-02-28": "600", "llm:tokens:gemini:2026-02-28": "200000"})
    assert out["gemini"]["requests_used"] == 600
    assert out["gemini"]["requests_pct"] == 50.0
    assert out["gemini"]["tokens_pct"] == 25.0


def test_yesterdays_counters_are_not_today():
    out = _usage({"llm:requests:gemini:2026-02-27": "999"})
    assert out["gemini"]["requests_used"] == 0


def test_every_configured_provider_is_reported():
    from app.ai.routing_policy import DAILY_LIMITS

    assert set(_usage({})) == set(DAILY_LIMITS)


def test_an_unreadable_counter_is_raised_not_reported_as_zero():
    """A Redis error read as "0 used", and /budget then showed headroom
    nobody had measured."""
    import pytest

    r = MagicMock()
    r.get.side_effect = ConnectionError("redis blip")
    with (
        patch.object(metrics.datetime, "datetime", _FrozenDatetime),
        patch("app.core.redis_client.get_redis", return_value=r),
        pytest.raises(ConnectionError),
    ):
        metrics.get_usage_today()


def test_budget_says_unreadable_rather_than_zero():
    with patch("app.core.redis_client.get_redis", side_effect=RuntimeError("down")):
        out = metrics.format_budget_comment()
    assert "could not be read" in out and "not a report of zero use" in out


def test_labels_name_the_configured_model_not_llama(monkeypatch):
    monkeypatch.setenv("LLM_PRIMARY_MODEL", "openai/gpt-oss-120b")
    out = _usage({})
    assert out["groq_70b"]["label"] == "Groq · openai/gpt-oss-120b"
    assert "Llama" not in " ".join(v["label"] for v in out.values())


def test_the_limits_are_labelled_as_assumptions():
    with patch("app.core.redis_client.get_redis", return_value=_redis({})):
        out = metrics.format_budget_comment()
    assert "not read from the provider" in out and "LLM_DAILY_LIMIT_" in out


def test_a_limit_can_be_set_to_the_accounts_plan(monkeypatch):
    from app.ai import routing_policy

    monkeypatch.setenv("LLM_DAILY_LIMIT_GROQ_70B_TOKENS", "200000")
    monkeypatch.setenv("LLM_DAILY_LIMIT_GEMINI_REQUESTS", "nonsense")
    limits = routing_policy._daily_limits()
    assert limits["groq_70b"]["tokens"] == 200_000
    assert limits["gemini"]["requests"] == 1_200, "a typo keeps the default"
