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
            return MagicMock(critical_count=0, total_count=3)

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
        report = MagicMock(critical_count=3, total_count=9)

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
                return_value=MagicMock(critical_count=0, total_count=0),
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
                return_value=MagicMock(critical_count=0, total_count=0),
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
    @pytest.fixture(autouse=True)
    def _configured(self, monkeypatch):
        from cryptography.fernet import Fernet

        monkeypatch.setenv("MEMORY_BACKUP_KEY", Fernet.generate_key().decode())
        monkeypatch.setenv("MEMORY_BACKUP_REPO", "o/private-backup")
        monkeypatch.setenv("MEMORY_BACKUP_TOKEN", "ghp_test")

    def test_existing_memory_is_never_overwritten(self, redis):
        """The whole safety argument. A restart during normal operation, a
        second worker booting, or a partially-warm instance must all be no-ops."""
        with (
            patch("app.intelligence.memory.known_repos", return_value=["o/r"]),
            patch.object(MB, "restore_from_github") as restore,
        ):
            assert MB.maybe_restore_on_boot() == 0
        restore.assert_not_called()

    def test_it_restores_when_memory_is_empty(self, redis):
        with (
            patch("app.intelligence.memory.known_repos", return_value=[]),
            patch.object(MB, "restore_from_github", return_value=4) as restore,
        ):
            assert MB.maybe_restore_on_boot() == 4
        restore.assert_called_once()

    def test_it_refuses_when_it_cannot_prove_memory_is_empty(self, redis):
        """'Could not read memory' is not 'memory is empty'."""
        with (
            patch("app.intelligence.memory.known_repos", side_effect=Exception("redis down")),
            patch.object(MB, "restore_from_github") as restore,
        ):
            assert MB.maybe_restore_on_boot() == 0
        restore.assert_not_called()

    def test_only_one_worker_restores(self, redis):
        with (
            patch("app.intelligence.memory.known_repos", return_value=[]),
            patch.object(MB, "restore_from_github", return_value=1) as restore,
        ):
            [MB.maybe_restore_on_boot() for _ in range(4)]
        assert restore.call_count == 1

    def test_an_unconfigured_destination_is_a_silent_no_op(self, monkeypatch, redis):
        monkeypatch.delenv("MEMORY_BACKUP_REPO", raising=False)
        with patch.object(MB, "restore_from_github") as restore:
            assert MB.maybe_restore_on_boot() == 0
        restore.assert_not_called()

    def test_nothing_on_a_timer_can_trigger_a_restore(self):
        """Export is scheduled; restore is not. The maintenance module must not
        reference the restore path at all."""
        import ast
        import pathlib

        tree = ast.parse(pathlib.Path("app/core/maintenance.py").read_text(encoding="utf-8"))
        names = {
            node.attr if isinstance(node, ast.Attribute) else node.id
            for node in ast.walk(tree)
            if isinstance(node, (ast.Name, ast.Attribute))
        }
        assert "restore_from_github" not in names
        assert "import_encrypted" not in names
        assert "maybe_restore_on_boot" not in names


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
