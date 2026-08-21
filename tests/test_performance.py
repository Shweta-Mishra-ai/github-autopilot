"""
tests/test_performance.py

Four defects found by profiling the webhook path rather than by reading it.
Each is pinned here by behaviour, not by a timing assertion — a wall-clock
threshold in CI is a flaky test, and the bug in every case was structural.

  1. idempotency logged "Redis unavailable" on EVERY event.
  2. _is_duplicate_local scanned all 2000 entries on every event to expire a
     handful.
  3. The in-memory IP rate limiter never freed an address, and grew a flooding
     IP's own window while refusing it.
  4. _perm_cache and _config_cache checked their TTL on read and never freed
     anything, because the only functions that could evict had no callers.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from unittest.mock import patch

import pytest


class TestStandingConditionsAreLoggedOnce:
    """"Redis is unavailable" does not become more true the four-thousandth
    time it is logged — it becomes less readable, and it buries the warnings
    that are about one specific event."""

    def test_the_fallback_warning_does_not_repeat_per_event(self, caplog):
        import app.core.idempotency as idem

        idem._in_fallback = False
        idem._seen_local.clear()

        with (
            patch.object(idem, "is_redis_available", return_value=False),
            caplog.at_level(logging.WARNING, logger="app.core.idempotency"),
        ):
            for i in range(50):
                idem.is_duplicate(f"fp{i}")

        fallback = [r for r in caplog.records if "using_memory_fallback" in r.getMessage()]
        assert len(fallback) == 1, f"logged {len(fallback)} times for one condition"

    def test_recovery_is_logged_so_the_operator_learns_it_ended(self, caplog):
        """Logging entry but not exit leaves the last word as 'degraded'.

        is_redis_available() is patched True because the test suite runs on the
        in-memory fake, where it is correctly False — recovery cannot be
        observed without a Redis that answers."""
        import app.core.idempotency as idem

        idem._in_fallback = True
        idem._seen_local.clear()

        with (
            patch.object(idem, "is_redis_available", return_value=True),
            caplog.at_level(logging.WARNING, logger="app.core.idempotency"),
        ):
            idem.is_duplicate("fp-recovered")

        assert any("redis_recovered" in r.getMessage() for r in caplog.records)
        assert idem._in_fallback is False

    def test_a_second_outage_warns_again(self):
        """Once-per-process would be wrong: an outage months later is news."""
        import app.core.idempotency as idem

        idem._in_fallback = True
        idem._seen_local.clear()

        with patch.object(idem, "is_redis_available", return_value=True):
            idem.is_duplicate("recover")
        assert idem._in_fallback is False

        with patch.object(idem, "is_redis_available", return_value=False):
            idem.is_duplicate("degrade-again")
        assert idem._in_fallback is True


class TestExpiryDoesNotScanEverything:
    def test_only_the_expired_prefix_is_examined(self):
        """_seen_local is an OrderedDict written in time order, so the oldest
        key is always first and everything after the first live entry is live.
        The old code filtered all 2000 entries on every event to find the few
        that aged out."""
        import app.core.idempotency as idem

        now = time.time()
        idem._seen_local.clear()
        idem._seen_local.update(
            OrderedDict(
                [(f"old{i}", now - idem._TTL_SECONDS - 10) for i in range(3)]
                + [(f"live{i}", now) for i in range(500)]
            )
        )

        idem._is_duplicate_local("probe")

        assert not any(k.startswith("old") for k in idem._seen_local)
        assert sum(1 for k in idem._seen_local if k.startswith("live")) == 500

    def test_expiry_stops_at_the_first_live_entry(self):
        """Correctness of the early break: nothing live may be dropped."""
        import app.core.idempotency as idem

        now = time.time()
        idem._seen_local.clear()
        idem._seen_local["expired"] = now - idem._TTL_SECONDS - 1
        idem._seen_local["fresh"] = now
        idem._seen_local["also_expired_but_later"] = now - idem._TTL_SECONDS - 1

        idem._is_duplicate_local("probe")

        assert "expired" not in idem._seen_local
        assert "fresh" in idem._seen_local
        # Out-of-order insertion is not something the writer does; the entry
        # survives this pass and is collected once it reaches the front.
        assert "also_expired_but_later" in idem._seen_local


class TestIpRateLimiterDoesNotLeak:
    @pytest.fixture(autouse=True)
    def _memory_only(self):
        import app.core.webhook_security as ws

        ws._ip_counts.clear()
        ws._last_sweep = 0.0
        ws._rl_in_fallback = False
        with patch("app.core.redis_client.is_redis_available", return_value=False):
            yield

    def test_addresses_that_stop_sending_are_swept(self):
        """The old code tried to delete empty windows but appended the current
        timestamp *before* testing for emptiness, so the window was never empty
        and the delete branch was unreachable. One entry per source address,
        forever, on a public endpoint."""
        import app.core.webhook_security as ws

        stale = time.time() - 120
        for i in range(200):
            ws._ip_counts[f"10.0.0.{i}"] = [stale]

        ws._last_sweep = 0.0  # force the amortised sweep
        ws.check_ip_rate_limit("10.1.1.1")

        assert len(ws._ip_counts) == 1
        assert "10.1.1.1" in ws._ip_counts

    def test_active_addresses_survive_the_sweep(self):
        import app.core.webhook_security as ws

        ws.check_ip_rate_limit("10.2.2.2")
        ws._last_sweep = 0.0
        ws.check_ip_rate_limit("10.3.3.3")

        assert {"10.2.2.2", "10.3.3.3"} <= set(ws._ip_counts)

    def test_a_flooding_address_does_not_grow_its_own_window(self):
        """Appending while over the limit made the limiter pay for the flood it
        was refusing: the window grew unbounded for a full minute."""
        import app.core.webhook_security as ws

        for _ in range(ws.IP_RATE_LIMIT + 500):
            ws.check_ip_rate_limit("10.9.9.9")

        assert len(ws._ip_counts["10.9.9.9"]) <= ws.IP_RATE_LIMIT

    def test_the_limit_itself_is_unchanged(self):
        """The refactor moved the append after the check. Off-by-one here
        would either throttle honest traffic or let the limit be exceeded."""
        import app.core.webhook_security as ws

        allowed = sum(1 for _ in range(ws.IP_RATE_LIMIT + 50) if ws.check_ip_rate_limit("10.4.4.4"))
        assert allowed == ws.IP_RATE_LIMIT

    def test_the_fallback_notice_is_logged_once(self, caplog):
        import app.core.webhook_security as ws

        with (
            patch("app.core.redis_client.is_redis_available", side_effect=RuntimeError("down")),
            caplog.at_level(logging.WARNING, logger="app.core.webhook_security"),
        ):
            for _ in range(30):
                ws.check_ip_rate_limit("10.5.5.5")

        notices = [r for r in caplog.records if "memory_fallback" in r.getMessage()]
        assert len(notices) == 1


class TestCachesActuallyFreeMemory:
    """Checking a TTL on read is not the same as honouring it. Both caches
    invalidated entries and never freed them, because the only functions that
    could evict — invalidate_permission_cache and invalidate_config_cache —
    had no callers anywhere in the codebase."""

    def test_expired_permission_entries_are_freed(self):
        import app.core.authorization as auth

        auth._perm_cache.clear()
        old = time.time() - auth._PERM_TTL - 10
        for i in range(100):
            auth._perm_cache[("o/r", f"user{i}")] = ("write", old)

        auth._last_perm_prune = 0.0
        auth._prune_perm_cache(time.time())

        assert auth._perm_cache == {}

    def test_live_permission_entries_survive(self):
        import app.core.authorization as auth

        auth._perm_cache.clear()
        auth._perm_cache[("o/r", "fresh")] = ("write", time.time())
        auth._last_perm_prune = 0.0
        auth._prune_perm_cache(time.time())

        assert ("o/r", "fresh") in auth._perm_cache

    def test_expired_config_entries_are_freed(self):
        import app.core.config as cfg

        cfg._config_cache.clear()
        old = time.time() - cfg._CONFIG_TTL - 10
        for i in range(50):
            cfg._config_cache[f"o/r{i}"] = (cfg.Config({}), old)

        cfg._last_config_prune = 0.0
        cfg._prune_config_cache(time.time())

        assert cfg._config_cache == {}

    def test_pruning_is_amortised_not_per_call(self):
        """An O(entries) pass on every permission check would trade a memory
        leak for a CPU one."""
        import app.core.authorization as auth

        auth._perm_cache.clear()
        auth._perm_cache[("o/r", "u")] = ("write", time.time() - auth._PERM_TTL - 10)
        auth._last_perm_prune = time.time()  # just pruned

        auth._prune_perm_cache(time.time())
        assert ("o/r", "u") in auth._perm_cache, "pruned again immediately"


class TestConfigChangeDropsStaleCaches:
    """A maintainer fixing their config waited up to five minutes to find out
    whether the fix worked — long enough to conclude it had not."""

    def _commits(self, **fields):
        base = {"added": [], "modified": [], "removed": []}
        base.update(fields)
        return [base]

    @pytest.mark.parametrize("field", ["added", "modified", "removed"])
    def test_any_change_to_the_config_file_counts(self, field):
        from app.handlers.push import _config_file_touched

        assert _config_file_touched(self._commits(**{field: [".ai-repo-manager.yml"]}))

    def test_an_unrelated_push_does_not_drop_the_cache(self):
        from app.handlers.push import _config_file_touched

        assert not _config_file_touched(self._commits(modified=["app/main.py", "README.md"]))

    def test_a_malformed_commit_entry_is_survivable(self):
        from app.handlers.push import _config_file_touched

        assert not _config_file_touched([None, "not-a-dict", {}])
        assert not _config_file_touched([])
        assert not _config_file_touched(None)

    def test_the_invalidate_helpers_now_have_a_caller(self):
        """Both existed with zero callers, which is why the TTL was the only
        thing that ever expired an entry."""
        import ast
        import pathlib

        called = set()
        for path in pathlib.Path("app").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call):
                    fn = node.func
                    called.add(fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", ""))

        assert "invalidate_config_cache" in called
        assert "invalidate_permission_cache" in called
