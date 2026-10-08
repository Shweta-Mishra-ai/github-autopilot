"""Numeric settings must never stop the app from starting (2026-10-08 audit)."""

import pytest

from app.core.env import env_int


@pytest.mark.parametrize("raw", ["", "  ", "6 workers", "1e3", "abc"])
def test_malformed_values_fall_back(monkeypatch, raw):
    monkeypatch.setenv("SOME_SETTING", raw)
    assert env_int("SOME_SETTING", 7, minimum=1) == 7


def test_out_of_range_falls_back(monkeypatch):
    monkeypatch.setenv("SOME_SETTING", "0")
    assert env_int("SOME_SETTING", 6, minimum=1) == 6


def test_a_valid_value_is_used(monkeypatch):
    monkeypatch.setenv("SOME_SETTING", " 12 ")
    assert env_int("SOME_SETTING", 6, minimum=1) == 12


def test_import_time_settings_use_the_safe_parser():
    import pathlib
    import re

    for path in ("app/core/thread_pool.py", "app/core/event_queue.py", "app/core/redis_client.py"):
        src = pathlib.Path(path).read_text()
        assert not re.search(r"int\(os\.environ", src), path


def test_a_worker_process_is_not_registered_as_web(monkeypatch):
    from unittest.mock import patch

    from app.core import process_guard

    monkeypatch.setenv("AUTOPILOT_PROCESS_ROLE", "worker")
    with patch("app.core.redis_client.get_redis") as get:
        process_guard.register_this_process()
    get.assert_not_called()


def test_health_refreshes_the_registration(monkeypatch):
    from unittest.mock import patch

    from app.core import process_guard

    monkeypatch.delenv("AUTOPILOT_PROCESS_ROLE", raising=False)
    with patch.object(process_guard, "register_this_process") as reg, \
         patch.object(process_guard, "active_process_count", return_value=1):
        assert process_guard.verdict()[0] == "ok"
    reg.assert_called_once()


def test_the_event_time_header_is_read_whatever_its_case():
    import time

    from app.core.webhook_security import verify_timestamp

    old = str(int(time.time()) - 10 * 86400)
    assert verify_timestamp({"X-Github-Event-Time": old}) is False
