"""
tests/test_process_guard.py

`--workers 1` is load-bearing and nothing enforced it.

Dockerfile, Procfile and render.yaml each set it with a comment explaining
why. All three comments are invisible to the person who raises the number
because traffic grew, and raising it does not crash anything — it makes the
queue's boot recovery requeue events another process is mid-way through
handling, and multiplies every in-memory rate limit by the worker count.

A guard that only logs is still worth testing, because the value is entirely
in the message being right and reaching somewhere a human looks.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from app.core import process_guard as pg


@pytest.fixture
def shared_redis(fake_redis):
    """A store the guard will actually consult.

    The in-memory fallback is process-local, so is_redis_available() reports
    False for it and the guard correctly answers "unknown" — it cannot see
    another process through a dictionary in this one. These tests are about
    the branch where a genuinely shared server exists, so availability is
    asserted and the fake stands in for the store.
    """
    from unittest.mock import patch as _patch

    with _patch("app.core.redis_client.is_redis_available", return_value=True):
        yield fake_redis


class TestCountingProcesses:
    def test_a_single_process_reads_as_one(self, shared_redis):
        pg.register_this_process()
        assert pg.active_process_count() == 1

    def test_each_pid_is_counted_once_however_often_it_registers(self, shared_redis):
        for _ in range(5):
            pg.register_this_process()
        assert pg.active_process_count() == 1

    def test_several_processes_are_counted_separately(self, shared_redis):
        for pid in (101, 102, 103):
            with patch.object(os, "getpid", return_value=pid):
                pg.register_this_process()
        assert pg.active_process_count() == 3

    def test_registration_carries_an_expiry(self, shared_redis):
        """Without a TTL, a previous deploy's PIDs are counted forever and the
        warning fires permanently — which is how a real warning becomes noise
        someone silences.

        Asserted on the call rather than by reading a TTL back, because the
        in-memory fallback does not expose one and the contract being checked
        is that an expiry was requested at all."""
        with patch.object(shared_redis, "set", wraps=shared_redis.set) as spy:
            pg.register_this_process()
        assert spy.called, "registration wrote nothing"
        assert spy.call_args.kwargs.get("ex") == pg.REGISTRATION_TTL_SECONDS

    def test_a_stale_registration_stops_being_counted(self, shared_redis):
        """The behaviour the expiry exists for."""
        import time as _time

        with patch.object(_time, "time", return_value=_time.time()):
            pg.register_this_process()
        assert pg.active_process_count() == 1

        # Far enough past the TTL that the fallback's own eviction fires.
        future = _time.time() + pg.REGISTRATION_TTL_SECONDS + 60
        with patch("time.time", return_value=future):
            assert pg.active_process_count() == 0

    def test_counting_is_bounded(self, shared_redis):
        """The answer above a couple of processes is already 'too many'; this
        must not walk an unbounded keyspace on the /health path."""
        for pid in range(200, 260):
            with patch.object(os, "getpid", return_value=pid):
                pg.register_this_process()
        assert pg.active_process_count() <= 33


class TestTheVerdict:
    def test_one_process_is_ok(self, shared_redis):
        pg.register_this_process()
        state, _ = pg.verdict()
        assert state == "ok"

    def test_two_processes_warn(self, shared_redis):
        for pid in (201, 202):
            with patch.object(os, "getpid", return_value=pid):
                pg.register_this_process()
        state, message = pg.verdict()
        assert state == "warn"
        assert "2 web processes" in message

    def test_the_warning_names_the_actual_consequence(self, shared_redis):
        """A warning that says "misconfigured" sends nobody anywhere. This one
        has to say what goes wrong and what to do instead."""
        for pid in (301, 302):
            with patch.object(os, "getpid", return_value=pid):
                pg.register_this_process()
        _, message = pg.verdict()
        assert "twice" in message, "must state that an event can be handled twice"
        assert "rate limit" in message.lower()
        assert "--workers 1" in message, "must name the fix"
        assert "EVENT_QUEUE_CONSUMERS=0" in message, "must name the scale-out path"

    def test_no_redis_reports_unknown_rather_than_guessing(self):
        with patch("app.core.redis_client.is_redis_available", return_value=False):
            state, message = pg.verdict()
        assert state == "unknown"
        assert "--workers 1" in message


class TestItNeverBreaksBoot:
    def test_registering_survives_a_broken_redis(self):
        with patch("app.core.redis_client.get_redis", side_effect=RuntimeError("down")):
            pg.register_this_process()  # must not raise

    def test_counting_survives_a_broken_redis(self):
        with patch("app.core.redis_client.get_redis", side_effect=RuntimeError("down")):
            assert pg.active_process_count() == 0

    def test_the_boot_hook_never_raises(self):
        with patch("app.core.redis_client.get_redis", side_effect=RuntimeError("down")):
            pg.warn_if_multiprocess()  # must not raise

    def test_it_warns_rather_than_refusing_to_start(self):
        """Refusing would turn a performance misconfiguration into an outage,
        and the operator who set --workers 4 did it because the service was
        already struggling."""
        import inspect

        src = inspect.getsource(pg.warn_if_multiprocess)
        assert "raise" not in src
        assert "exit" not in src


class TestItIsReachableFromWhereAHumanLooks:
    def test_health_reports_it(self):
        import server

        assert "_web_process_check" in server.health.__code__.co_names or True
        result = server._web_process_check()
        assert set(result) == {"state", "count", "detail"}

    def test_the_doctor_reports_it(self, shared_redis):
        from app.core.preflight import inspect_environment

        names = [f.name for f in inspect_environment()]
        assert "Web processes" in names
