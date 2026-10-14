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
    assert set(_usage({})) == set(metrics.PROVIDER_LIMITS)


def test_one_unreadable_counter_does_not_lose_the_others():
    def get(key):
        if "gemini" in key:
            raise ConnectionError("redis blip")
        return "10" if key.startswith("llm:requests:openrouter") else None

    r = MagicMock()
    r.get.side_effect = get
    with (
        patch.object(metrics.datetime, "datetime", _FrozenDatetime),
        patch("app.core.redis_client.get_redis", return_value=r),
    ):
        out = metrics.get_usage_today()
    assert out["gemini"]["requests_used"] == 0
    assert out["openrouter"]["requests_used"] == 10


def test_no_redis_returns_empty_rather_than_raising():
    with patch("app.core.redis_client.get_redis", side_effect=RuntimeError("down")):
        assert metrics.get_usage_today() == {}
