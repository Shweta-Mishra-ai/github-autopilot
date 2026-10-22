"""
tests/test_maintenance.py

The 15-day sweep and the encrypted memory backup.

Two properties carry the whole design and are what these tests defend:

  1. A schedule measured in days cannot live in a `sleep()`. This app runs on a
     free tier that restarts on deploy, on idle, and on the host's schedule,
     and every restart puts a sleep back to zero — a 15-day timer would never
     fire once. The due time lives in Redis and the thread only checks it.

  2. Restore overwrites live memory, so it may only run when there is nothing
     to overwrite. That is enforced by construction (memory empty?) rather than
     by being careful about when it is called.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

import app.core.maintenance as M
import app.core.memory_backup as MB
from app.core import installations as INST


@pytest.fixture
def redis(monkeypatch):
    """A live _FakeRedis, isolated per test by the autouse singleton reset."""
    import app.core.redis_client as rc

    monkeypatch.setenv("REDIS_URL", "")
    rc.reset_client()
    return rc.get_redis()


# ── The cadence survives restarts ─────────────────────────────────────────────


class TestDueTimeNotSleep:
    def test_first_boot_schedules_ahead_and_does_not_run(self, redis):
        """A cold deploy has nothing worth scanning, and a wiped Redis must not
        become a trigger for an unscheduled full sweep of every repository."""
        assert M.claim_due_run() is False
        assert int(redis.get(M._DUE_KEY)) > time.time()

    def test_a_restart_does_not_reset_the_clock(self, redis):
        """The point of storing the due time. Two 'boots' in a row must not
        push the deadline out — that is how a sleep-based timer never fires."""
        M.claim_due_run()
        first_due = int(redis.get(M._DUE_KEY))

        for _ in range(5):  # five restarts
            M.claim_due_run()

        assert int(redis.get(M._DUE_KEY)) == first_due

    def test_it_runs_once_the_due_time_has_passed(self, redis):
        redis.set(M._DUE_KEY, str(int(time.time()) - 10))
        assert M.claim_due_run() is True

    def test_the_due_time_advances_before_the_work_starts(self, redis):
        """A sweep takes minutes and can die halfway. Advancing afterwards
        would make a crashing run retry every hour, forever."""
        redis.set(M._DUE_KEY, str(int(time.time()) - 10))
        M.claim_due_run()
        assert int(redis.get(M._DUE_KEY)) > time.time()

    def test_only_one_process_wins_a_due_run(self, redis):
        """Gunicorn runs several workers and each imports server.py. Two
        winners means two full sweeps and two alerts for one cycle."""
        redis.set(M._DUE_KEY, str(int(time.time()) - 10))
        results = [M.claim_due_run() for _ in range(4)]
        assert results.count(True) == 1

    def test_a_redis_failure_means_do_not_run(self):
        """Fails closed. The pass writes to GitHub and spends provider quota,
        so 'I could not check' must never mean 'go ahead'."""
        with patch.object(M, "_redis", side_effect=Exception("redis down")):
            assert M.claim_due_run() is False

    def test_interval_defaults_to_fifteen_days(self, monkeypatch):
        monkeypatch.delenv(M.INTERVAL_DAYS_ENV, raising=False)
        assert M.interval_seconds() == 15 * 24 * 3600

    @pytest.mark.parametrize("value", ["0", "0.001", "-5", "not-a-number"])
    def test_a_bad_interval_cannot_become_a_request_loop(self, monkeypatch, value):
        monkeypatch.setenv(M.INTERVAL_DAYS_ENV, value)
        assert M.interval_seconds() >= 24 * 3600


# ── The sweep itself ──────────────────────────────────────────────────────────


class TestThePass:
    def test_one_repo_failing_does_not_stop_the_others(self, redis):
        INST.remember_installation("o/good", 1)
        INST.remember_installation("o/bad", 2)

        def scan(repo, token):
            if repo == "o/bad":
                raise RuntimeError("403")
            return MagicMock(critical_count=0, total_count=3, scanned_nothing=False, errors=[])

        with (
            patch("app.github.auth.get_installation_token", return_value="tok"),
            patch("app.security.scanner.run_security_scan", side_effect=scan),
            patch.object(MB, "run_backup_once", return_value=True),
        ):
            result = M.run_pass()

        assert result["repos_scanned"] == 1
        assert result["repos_failed"] == 1

    def test_a_scan_failure_never_costs_the_backup(self, redis):
        """The backup is the half that protects data, so it runs last and
        unconditionally."""
        INST.remember_installation("o/r", 1)
        with (
            patch("app.github.auth.get_installation_token", side_effect=Exception("boom")),
            patch.object(MB, "run_backup_once", return_value=True) as backup,
        ):
            result = M.run_pass()

        assert backup.called
        assert result["memory_backed_up"] is True

    def test_critical_findings_alert_with_the_right_shape(self, redis):
        """notify_vulnerability() takes one package and CVE. Passing a
        repository-level count through it would render 'Package `3 critical
        findings` has a known vulnerability'."""
        INST.remember_installation("o/r", 1)
        report = MagicMock(critical_count=3, total_count=9, scanned_nothing=False, errors=[])

        with (
            patch("app.github.auth.get_installation_token", return_value="tok"),
            patch("app.security.scanner.run_security_scan", return_value=report),
            patch.object(MB, "run_backup_once", return_value=False),
            patch("app.github.notifications.notify") as notify,
        ):
            M.run_pass()

        assert notify.called
        kwargs = notify.call_args.kwargs
        assert kwargs["repo"] == "o/r"
        assert kwargs["severity"] == "critical"
        assert "3" in kwargs["message"]

    def test_clean_repos_raise_no_alert(self, redis):
        INST.remember_installation("o/r", 1)
        with (
            patch("app.github.auth.get_installation_token", return_value="tok"),
            patch(
                "app.security.scanner.run_security_scan",
                return_value=MagicMock(critical_count=0, total_count=0, scanned_nothing=False, errors=[]),
            ),
            patch.object(MB, "run_backup_once", return_value=True),
            patch("app.github.notifications.notify") as notify,
        ):
            M.run_pass()
        notify.assert_not_called()

    def test_the_repo_count_per_run_is_bounded(self, redis):
        """One busy deployment must not turn the pass into an hour of API
        calls. The overflow is picked up next cycle, not dropped."""
        for i in range(M.MAX_REPOS_PER_RUN + 10):
            INST.remember_installation(f"o/r{i}", i + 1)

        with (
            patch("app.github.auth.get_installation_token", return_value="tok"),
            patch(
                "app.security.scanner.run_security_scan",
                return_value=MagicMock(critical_count=0, total_count=0, scanned_nothing=False, errors=[]),
            ) as scan,
            patch.object(MB, "run_backup_once", return_value=True),
        ):
            M.run_pass()

        assert scan.call_count == M.MAX_REPOS_PER_RUN

    def test_tick_is_a_no_op_when_disabled(self, monkeypatch):
        monkeypatch.setenv(M.ENABLED_ENV, "0")
        with patch.object(M, "run_pass") as run:
            assert M.tick() is None
        run.assert_not_called()


# ── Restore may never overwrite live memory ───────────────────────────────────


class TestRestoreIsNonDestructive:
    """
    Render's free Redis keeps nothing on disk and can be restarted — emptied —
    at any time while the app runs. Restore used to run only at app boot and
    only when memory was empty, so a wipe mid-life was never restored, and the
    first memory written after it let the next backup replace the good one.

    Now a marker records that this Redis's memory is accounted for. While it is
    missing, reconcile() MERGES the backup in (never deleting anything) and
    backups refuse to run.
    """

    @pytest.fixture(autouse=True)
    def _configured(self, monkeypatch):
        from cryptography.fernet import Fernet

        monkeypatch.setenv("MEMORY_BACKUP_KEY", Fernet.generate_key().decode())
        monkeypatch.setenv("MEMORY_BACKUP_REPO", "o/private-backup")
        monkeypatch.setenv("MEMORY_BACKUP_TOKEN", "ghp_test")

    @staticmethod
    def _backup_holding(text):
        """GitHub serving an encrypted backup that holds one memory for o/r."""
        import base64

        from app.intelligence import memory

        memory.remember("o/r", text)
        blob = MB.export_encrypted(["o/r"])
        memory.clear("o/r")
        return {"content": base64.b64encode(blob).decode(), "sha": "s", "size": len(blob)}

    def test_a_wipe_mid_life_is_merged_back_keeping_what_arrived_since(self, redis):
        from app.intelligence import memory

        served = self._backup_holding("decision from before the wipe")
        memory.remember("o/r", "written after the wipe")  # memory is NOT empty now
        with patch("app.github.client.gh_get", return_value=served):
            assert MB.reconcile() == 1
        texts = [i.text for i in memory.recall("o/r", "decision wipe written before after", top_k=10)]
        assert "decision from before the wipe" in texts and "written after the wipe" in texts

    def test_a_reconciled_redis_is_left_alone(self, redis):
        redis.set(MB.WIPE_MARKER, "1")
        with patch("app.github.client.gh_get") as get:
            assert MB.reconcile() == 0
        get.assert_not_called()

    def test_no_backup_yet_is_not_a_failure(self, redis):
        from app.github.client import GitHubError

        with patch("app.github.client.gh_get", side_effect=GitHubError("Not found", 404)):
            assert MB.reconcile() == 0
        assert redis.exists(MB.WIPE_MARKER), "backups may start"

    def test_an_unreachable_backup_pauses_backups(self, redis):
        with patch("app.github.client.gh_get", side_effect=ConnectionError("down")):
            assert MB.reconcile() == 0
        assert not redis.exists(MB.WIPE_MARKER)
        with patch.object(MB, "backup_to_github") as push:
            assert MB.run_backup_once() is False
        push.assert_not_called()

    def test_the_merge_does_not_duplicate_on_a_second_pass(self, redis):
        from app.intelligence import memory

        served = self._backup_holding("only once")
        with patch("app.github.client.gh_get", return_value=served):
            MB.reconcile()
            redis.delete(MB.WIPE_MARKER)
            redis.delete(MB._RESTORE_LOCK_KEY)
            MB.reconcile()
        assert len(redis.lrange("mem:o/r", 0, -1)) == 1
        assert memory.remember("o/r", "only once") is False, "the dedup index was rebuilt"

    def test_only_one_worker_reconciles(self, redis):
        with patch.object(MB, "_fetch_backup", return_value=None) as fetch:
            redis.set(MB._RESTORE_LOCK_KEY, "1")  # another worker holds it
            assert MB.reconcile() == 0
        fetch.assert_not_called()

    def test_an_unconfigured_destination_is_a_silent_no_op(self, monkeypatch, redis):
        monkeypatch.delenv("MEMORY_BACKUP_REPO", raising=False)
        with patch.object(MB, "_fetch_backup") as fetch:
            assert MB.maybe_restore_on_boot() == 0
        fetch.assert_not_called()

    def test_the_hourly_tick_reconciles_and_backs_up_daily(self, redis):
        with patch.object(MB, "reconcile", return_value=0) as rec, \
             patch.object(MB, "run_backup_once", return_value=True) as backup:
            first = M.memory_care(now=1_000_000)
            M.memory_care(now=1_000_000 + 3600)  # an hour later: not due
            M.memory_care(now=1_000_000 + 25 * 3600)  # a day later: due
        assert rec.call_count == 3
        assert first["backed_up"] is True and backup.call_count == 2


# ── Configuration must be all-or-nothing ──────────────────────────────────────


class TestBackupDestination:
    @pytest.mark.parametrize("missing", ["MEMORY_BACKUP_KEY", "MEMORY_BACKUP_REPO", "MEMORY_BACKUP_TOKEN"])
    def test_a_partial_configuration_is_not_a_backup(self, monkeypatch, missing):
        """A key with no destination encrypts something and drops it; a
        destination with no key would push plaintext if anything ever forgot to
        check. Neither counts as configured."""
        from cryptography.fernet import Fernet

        monkeypatch.setenv("MEMORY_BACKUP_KEY", Fernet.generate_key().decode())
        monkeypatch.setenv("MEMORY_BACKUP_REPO", "o/b")
        monkeypatch.setenv("MEMORY_BACKUP_TOKEN", "ghp_x")
        monkeypatch.delenv(missing, raising=False)

        assert MB.backup_destination() is None
        assert MB.run_backup_once() is False

    def test_the_path_has_a_default(self, monkeypatch):
        from cryptography.fernet import Fernet

        monkeypatch.setenv("MEMORY_BACKUP_KEY", Fernet.generate_key().decode())
        monkeypatch.setenv("MEMORY_BACKUP_REPO", "o/b")
        monkeypatch.setenv("MEMORY_BACKUP_TOKEN", "ghp_x")
        monkeypatch.delenv("MEMORY_BACKUP_PATH", raising=False)

        assert MB.backup_destination()[1] == MB.DEFAULT_BACKUP_PATH


# ── The installation registry the sweep depends on ────────────────────────────


class TestInstallationRegistry:
    def test_round_trip(self, redis):
        INST.remember_installation("o/r", 42)
        assert INST.installation_for("o/r") == 42
        assert INST.known_installations() == {"o/r": 42}

    def test_an_expired_entry_is_pruned_rather_than_returned_null(self, redis):
        """The caller schedules work. A repo it cannot authenticate to is not
        work it can do, so it must not appear at all."""
        INST.remember_installation("o/live", 1)
        INST.remember_installation("o/gone", 2)
        redis.delete(INST._entry_key("o/gone"))

        assert INST.known_installations() == {"o/live": 1}
        assert "o/gone" not in redis.smembers(INST._INDEX_KEY)

    def test_forget_removes_from_both_the_entry_and_the_index(self, redis):
        INST.remember_installation("o/r", 1)
        INST.forget_installation("o/r")
        assert INST.installation_for("o/r") is None
        assert INST.known_installations() == {}

    def test_a_missing_id_is_not_recorded(self, redis):
        assert INST.remember_installation("o/r", 0) is False
        assert INST.remember_installation("", 5) is False

    def test_no_token_is_ever_persisted(self):
        """Installation tokens expire in an hour and live in the in-process
        cache. Writing one to Redis would put a credential in a store this app
        treats as cache, outliving the token's own lifetime."""
        import pathlib

        src = pathlib.Path("app/core/installations.py").read_text(encoding="utf-8")
        assert "get_installation_token" not in src
        # The registry stores an integer id and nothing else. Any Redis write
        # here must be of the id or the index, never of a credential.
        assert "access_token" not in src
        assert INST.ENTRY_TTL_SECONDS > 15 * 24 * 3600, (
            "entries must outlive the maintenance cadence, or a quiet repo is "
            "dropped between two sweeps and silently stops being scanned"
        )

    def test_lookups_survive_a_redis_outage(self):
        with patch("app.core.redis_client.get_redis", side_effect=Exception("down")):
            assert INST.installation_for("o/r") is None
            assert INST.known_installations() == {}
            assert INST.remember_installation("o/r", 1) is False


# ── The repo index the backup and the sweep both read ─────────────────────────


class TestRepoIndexIsASet:
    """
    It was a list with dedup done by reading the whole thing back on every
    write: O(n) in the number of repos on the hottest path in memory.py — the
    same complexity that module's docstring says was removed from remember() —
    and not atomic, so two concurrent writers for a new repo both saw "absent"
    and both pushed it.
    """

    def test_repeated_writes_do_not_duplicate(self, redis):
        from app.intelligence import memory as M

        for _ in range(5):
            M._index_repo(redis, "o/r")
        assert M.known_repos() == ["o/r"]

    def test_indexing_does_not_read_the_whole_index(self, redis):
        """The O(n) part. sadd is one call; the old path did an lrange of every
        known repo before every single memory write."""
        from app.intelligence import memory as M

        r = MagicMock()
        M._index_repo(r, "o/r")
        r.sadd.assert_called_once()
        r.lrange.assert_not_called()

    def test_known_repos_is_sorted(self, redis):
        """The maintenance pass takes a bounded slice of this. An arbitrary set
        order would silently scan a different subset every cycle."""
        from app.intelligence import memory as M

        for name in ("o/c", "o/a", "o/b"):
            M._index_repo(redis, name)
        assert M.known_repos() == ["o/a", "o/b", "o/c"]

    def test_the_pre_720_list_is_migrated_once(self, redis):
        """Reading a set with LRANGE raises WRONGTYPE, so changing the type in
        place would have broken every running worker until it restarted. The
        set uses a new key and absorbs the old list on first read."""
        from app.intelligence import memory as M

        redis.lpush(M._LEGACY_INDEX_KEY, "o/old1", "o/old2")
        M._index_repo(redis, "o/new")

        assert M.known_repos() == ["o/new", "o/old1", "o/old2"]
        # Consumed, not left to be migrated again on every read.
        assert redis.lrange(M._LEGACY_INDEX_KEY, 0, -1) in ([], None)

    def test_clearing_a_repo_removes_it_from_the_index(self, redis):
        """Leaving the entry behind means known_repos() keeps reporting a repo
        with nothing in it, and the backup carries an empty record forever."""
        from app.intelligence import memory as M

        M._index_repo(redis, "o/r")
        M.clear("o/r")
        assert M.known_repos() == []

    def test_a_redis_outage_yields_an_empty_list_not_an_exception(self):
        from app.intelligence import memory as M

        with patch("app.core.redis_client.get_redis", side_effect=Exception("down")):
            assert M.known_repos() == []


class TestEveryRepoIsEventuallyScanned:
    """The sweep took sorted()[:25] every cycle (2026-10-08 audit)."""

    def test_successive_cycles_cover_every_repository(self, monkeypatch):
        from app.core import maintenance

        class KV(dict):
            def get(self, k):
                return super().get(k)

            def set(self, k, v, **kw):
                self[k] = v
                return True

        kv = KV()
        monkeypatch.setattr("app.core.redis_client.get_redis", lambda: kv)
        ordered = [(f"o/r{i:02d}", i) for i in range(60)]
        seen = set()
        for _ in range(3):
            batch = maintenance._this_cycles_batch(ordered)
            assert len(batch) == maintenance.MAX_REPOS_PER_RUN
            seen.update(r for r, _ in batch)
        assert seen == {r for r, _ in ordered}


class TestAPartialScanIsSaidToBePartial:
    """A repository with one of three sources unreadable was "ok" with nothing
    saying which were missed, and its 0 findings read as clean."""

    def test_the_missed_sources_are_recorded_and_counted(self):
        from unittest.mock import patch

        from app.core import maintenance
        from app.security.scanner import SecurityReport

        report = SecurityReport(repo="o/r", errors=["CodeQL (not enabled or no permission)"])
        with patch("app.github.auth.get_installation_token", return_value="t"), \
             patch("app.security.scanner.run_security_scan", return_value=report):
            record = maintenance.scan_repo("o/r", 1)
        assert record["ok"] is True
        assert record["missed"] == ["CodeQL"]

        with patch("app.core.installations.known_installations", return_value={"o/r": 1}), \
             patch.object(maintenance, "scan_repo", return_value=record), \
             patch("app.core.memory_backup.run_backup_once", return_value=True), \
             patch.object(maintenance, "_store_result"):
            result = maintenance.run_pass()
        assert result["repos_partial"] == ["o/r"]


def test_a_refused_backup_is_retried_next_tick_not_next_day(monkeypatch):
    import app.core.redis_client as rc
    from cryptography.fernet import Fernet

    monkeypatch.setenv("REDIS_URL", "")
    rc.reset_client()
    monkeypatch.setenv("MEMORY_BACKUP_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("MEMORY_BACKUP_REPO", "o/b")
    monkeypatch.setenv("MEMORY_BACKUP_TOKEN", "ghp_x")
    with patch.object(MB, "reconcile", return_value=0), \
         patch.object(MB, "run_backup_once", side_effect=[False, True]) as backup:
        M.memory_care(now=2_000_000)
        M.memory_care(now=2_000_000 + 3600)
    assert backup.call_count == 2
