"""
GitHub does not redeliver a failed webhook by itself
(docs.github.com/webhooks/using-webhooks/handling-failed-webhook-deliveries).
On a free tier the delivery that wakes a sleeping instance always fails, and a
full queue answers 503 — those events were lost. app/github/redelivery.py asks
GitHub to redeliver them.
"""

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.github import redelivery as R

NOW = 1_800_000_000.0


def _at(seconds_ago):
    return datetime.fromtimestamp(NOW - seconds_ago, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _d(id_, guid, code, ago=60, event="issue_comment"):
    return {"id": id_, "guid": guid, "status_code": code, "delivered_at": _at(ago), "event": event, "action": "created"}


def _get(pages):
    calls = []

    def get(url, headers=None, params=None, timeout=None):
        calls.append(params)
        page = pages[len(calls) - 1]
        resp = MagicMock(status_code=200)
        resp.json.return_value = page["items"]
        resp.headers = {"Link": page.get("link", "")}
        resp.raise_for_status.return_value = None
        return resp

    get.calls = calls
    return get


@pytest.fixture(autouse=True)
def _jwt():
    with patch("app.github.auth.get_jwt", return_value="jwt"):
        yield


def test_what_is_retried():
    assert R.retryable(0) and R.retryable(None) and R.retryable(429)
    assert R.retryable(502) and R.retryable(503) and R.retryable(504)
    for deliberate in (400, 401, 413, 404):
        assert not R.retryable(deliberate), deliberate


def test_only_unsucceeded_retryable_events_are_picked():
    deliveries = [
        _d(1, "cold-start", 0),                       # timed out waking up
        _d(2, "queue-full", 503),
        _d(3, "bad-signature", 401),                  # deliberate refusal
        _d(4, "ok", 202),
        _d(5, "fixed-by-hand", 503, ago=120), _d(6, "fixed-by-hand", 202, ago=30),
    ]
    picked = R.failed_guids(deliveries)
    assert set(picked) == {"cold-start", "queue-full"}


def test_a_sweep_redelivers_and_caps_attempts(fake_redis):
    get = _get([{"items": [_d(10, "lost", 0), _d(11, "fine", 202)]}])
    post = MagicMock(return_value=MagicMock(status_code=202))
    out = R.sweep(now=NOW, get=get, post=post)
    assert out["requested"] == 1
    assert post.call_args.args[0].endswith("/app/hook/deliveries/10/attempts")

    for _ in range(3):  # it keeps failing on our side
        R.sweep(now=NOW, get=_get([{"items": [_d(10, "lost", 0)]}]), post=post)
    assert post.call_count == R.MAX_ATTEMPTS, "never more than MAX_ATTEMPTS per event"


def test_paging_stops_at_the_window(fake_redis):
    old = R.WINDOW_SECONDS + 10
    get = _get([
        {"items": [_d(20, "a", 0)], "link": '<https://api.github.com/app/hook/deliveries?per_page=100&cursor=v1_9>; rel="next"'},
        {"items": [_d(21, "b", 503), _d(22, "c", 0, ago=old)],
         "link": '<https://api.github.com/app/hook/deliveries?per_page=100&cursor=v1_8>; rel="next"'},
        {"items": [_d(23, "d", 0)]},
    ])
    post = MagicMock(return_value=MagicMock(status_code=202))
    out = R.sweep(now=NOW, get=get, post=post)
    assert out["failed"] == 2  # "c" is outside the window, and "d" is never read
    assert len(get.calls) == 2 and get.calls[1]["cursor"] == "v1_9"


def test_a_github_error_is_raised_not_read_as_nothing_failed(fake_redis):
    def get(*a, **k):
        resp = MagicMock(status_code=401)
        resp.raise_for_status.side_effect = RuntimeError("401 Bad credentials")
        return resp

    with pytest.raises(RuntimeError):
        R.sweep(now=NOW, get=get, post=MagicMock())


def test_off_switch_and_missing_credentials(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_PRIVATE_KEY", "k")
    monkeypatch.setenv("WEBHOOK_REDELIVERY", "1")
    assert R.enabled()
    monkeypatch.setenv("WEBHOOK_REDELIVERY", "0")
    assert not R.enabled() and R.tick() is None
    monkeypatch.setenv("WEBHOOK_REDELIVERY", "1")
    monkeypatch.delenv("GITHUB_PRIVATE_KEY")
    assert not R.enabled()


def test_one_process_sweeps_at_a_time(fake_redis, monkeypatch):
    monkeypatch.setenv("WEBHOOK_REDELIVERY", "1")
    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_PRIVATE_KEY", "k")
    with patch.object(R, "sweep", return_value={"requested": 0}) as sweep:
        R.tick()
        R.tick()
    assert sweep.call_count == 1


def test_health_reports_the_last_sweep(fake_redis):
    import json

    import server

    fake_redis.set("redelivery:last_result", json.dumps({"at": 1, "failed": 2, "requested": 2}))
    headers = {"Authorization": f"Bearer {server.METRICS_TOKEN}"} if server.METRICS_TOKEN else {}
    body = server.app.test_client().get("/health", headers=headers).get_json()
    assert body["webhook_redelivery"]["last_sweep"]["requested"] == 2
