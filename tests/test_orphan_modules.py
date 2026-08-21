"""
tests/test_orphan_modules.py

Three modules were fully written, fully tested and never imported by anything:
app/core/cache.py, app/security/licenses.py and app/core/memory_backup.py.
Passing unit tests on an unreachable module prove nothing about the product —
the licence scanner had a formatter, a risk taxonomy and green tests, and the
bot had still never reported a single copyleft dependency to anyone.

These tests cover the reachability, not the internals: that /secfull actually
calls the licence scanner, that a licence failure cannot swallow the security
report, and that memory_backup has a real operator entrypoint.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import patch

import pytest


def _b64(text: str) -> dict:
    return {"content": base64.b64encode(text.encode()).decode(), "encoding": "base64"}


class TestLicenseScanReachesSecfull:
    def test_secfull_includes_the_license_section(self):
        from app.handlers.comments import security as SEC

        with (
            patch("app.security.scanner.run_security_scan") as scan,
            patch.object(SEC, "gh_get", return_value=_b64("flask==3.0.0\n")),
            patch(
                "app.security.licenses.check_package_license",
                return_value={"package": "flask", "license": "GPL-3.0", "risk": "copyleft"},
            ),
        ):
            scan.return_value.to_markdown.return_value = "## Security\n\nnothing"
            out = SEC.cmd_secfull("o/r", "tok")

        assert "License Compliance" in out
        assert "flask" in out
        # The security report is still present — the licence block is additive.
        assert "## Security" in out

    def test_missing_requirements_txt_is_not_an_error(self):
        """A repo with no requirements.txt gets the security report and no
        licence block at all — not an error block, which would read as a scan
        failure to the maintainer."""
        from app.handlers.comments import security as SEC

        with (
            patch("app.security.scanner.run_security_scan") as scan,
            patch.object(SEC, "gh_get", side_effect=Exception("404 Not Found")),
        ):
            scan.return_value.to_markdown.return_value = "## Security\n\nnothing"
            out = SEC.cmd_secfull("o/r", "tok")

        assert out.strip() == "## Security\n\nnothing"
        assert "License" not in out

    def test_a_license_scan_failure_never_hides_the_security_report(self):
        from app.handlers.comments import security as SEC

        with (
            patch("app.security.scanner.run_security_scan") as scan,
            patch.object(SEC, "gh_get", return_value=_b64("flask==3.0.0\n")),
            patch(
                "app.security.licenses.scan_requirements",
                side_effect=RuntimeError("pypi exploded"),
            ),
        ):
            scan.return_value.to_markdown.return_value = "## Security\n\ncritical finding"
            out = SEC.cmd_secfull("o/r", "tok")

        assert "critical finding" in out
        assert "Security scan failed" not in out


class TestLicenseScanIsBounded:
    def test_scan_stops_at_the_deadline_instead_of_checking_all_20(self):
        """Twenty serial PyPI lookups at a 5s timeout each is a 100-second
        worst case inside a webhook handler. The loop is bounded by wall clock
        as well as by count."""
        import app.security.licenses as L

        reqs = "\n".join(f"pkg{i}==1.0.0" for i in range(20))
        calls: list[str] = []

        def slow(package: str) -> dict:
            calls.append(package)
            return {"package": package, "license": "GPL-3.0", "risk": "copyleft"}

        # monotonic() is read once before the loop and once per iteration.
        ticks = iter([0.0] + [0.0, 1.0, 999.0] + [999.0] * 50)

        with (
            patch.object(L, "check_package_license", side_effect=slow),
            patch("time.monotonic", side_effect=lambda: next(ticks)),
        ):
            L.scan_requirements(reqs)

        assert len(calls) < 20, "deadline did not truncate the scan"

    def test_an_unchecked_package_is_absent_rather_than_reported_risky(self):
        """Truncation must not invent findings. A package the scan never
        reached is simply not in the report — reporting it as 'unknown' would
        be a false positive."""
        import app.security.licenses as L

        ticks = iter([0.0, 0.0, 999.0, 999.0, 999.0])
        with (
            patch.object(
                L,
                "check_package_license",
                return_value={"package": "a", "license": "GPL-3.0", "risk": "copyleft"},
            ),
            patch("time.monotonic", side_effect=lambda: next(ticks)),
        ):
            out = L.scan_requirements("a==1\nb==1\nc==1\n")

        assert [f["package"] for f in out] == ["a"]

    def test_permissive_packages_are_never_reported(self):
        import app.security.licenses as L

        with patch.object(
            L,
            "check_package_license",
            return_value={"package": "flask", "license": "BSD-3-Clause", "risk": "safe"},
        ):
            assert L.scan_requirements("flask==3.0.0\n") == []


class TestMemoryBackupHasAnOperatorEntrypoint:
    def test_genkey_prints_a_usable_fernet_key(self, capsys):
        from app.core.memory_backup import main

        assert main(["genkey"]) == 0
        key = capsys.readouterr().out.strip()

        from cryptography.fernet import Fernet

        Fernet(key.encode())  # raises if malformed

    def test_export_without_a_key_configured_exits_nonzero(self, monkeypatch, tmp_path):
        """Silent success with no backup written is the failure mode that
        matters here — the operator finds out at restore time."""
        from app.core.memory_backup import main

        monkeypatch.delenv("MEMORY_BACKUP_KEY", raising=False)
        assert main(["export", "--out", str(tmp_path / "b.bin")]) == 2

    def test_export_then_restore_round_trips(self, monkeypatch, tmp_path):
        from cryptography.fernet import Fernet

        import app.core.memory_backup as MB

        monkeypatch.setenv("MEMORY_BACKUP_KEY", Fernet.generate_key().decode())
        dest = tmp_path / "backup.bin"

        with patch.object(MB, "_dump_repos", return_value={"o/r": ['{"t":"note"}']}):
            assert MB.main(["export", "--out", str(dest), "--repo", "o/r"]) == 0

        assert dest.exists() and dest.stat().st_size > 0

        with patch.object(MB, "import_encrypted", return_value=1) as imp:
            assert MB.main(["restore", "--in", str(dest)]) == 0
        # Default is a replacing restore; --merge is the opt-in.
        assert imp.call_args.kwargs["overwrite"] is True

        with patch.object(MB, "import_encrypted", return_value=1) as imp:
            assert MB.main(["restore", "--in", str(dest), "--merge"]) == 0
        assert imp.call_args.kwargs["overwrite"] is False

    def test_restore_with_the_wrong_key_fails_loudly(self, monkeypatch, tmp_path):
        """Fernet is authenticated encryption, so a wrong key is detected
        rather than producing garbage. The CLI must surface that as a nonzero
        exit, not a cheerful 'Restored 0 repos'."""
        from cryptography.fernet import Fernet

        import app.core.memory_backup as MB

        monkeypatch.setenv("MEMORY_BACKUP_KEY", Fernet.generate_key().decode())
        dest = tmp_path / "backup.bin"
        with patch.object(MB, "_dump_repos", return_value={"o/r": []}):
            assert MB.main(["export", "--out", str(dest)]) == 0

        monkeypatch.setenv("MEMORY_BACKUP_KEY", Fernet.generate_key().decode())
        assert MB.main(["restore", "--in", str(dest)]) == 1

    def test_the_restore_path_has_no_automatic_caller(self):
        """
        Export is scheduled (app/core/maintenance.py); restore is not.

        The invariant is not "nothing imports this module" — that was true only
        while the feature did nothing. It is that no automatic path can reach
        the half that OVERWRITES memory. maybe_restore_on_boot() is allowed one
        caller, the boot path, because it no-ops unless memory is empty;
        restore_from_github() and import_encrypted() overwrite unconditionally
        and may only be reached from the CLI.
        """
        import ast
        import pathlib

        import app.core.memory_backup as MB

        unconditional = {"restore_from_github", "import_encrypted"}
        callers = []
        for path in list(pathlib.Path("app").rglob("*.py")) + [
            pathlib.Path("server.py"),
            pathlib.Path("worker.py"),
        ]:
            if path.name == "memory_backup.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call):
                    fn = node.func
                    name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                    if name in unconditional:
                        callers.append(f"{path}::{name}")

        assert callers == [], f"restore is reachable automatically from: {callers}"
        assert hasattr(MB, "main")

    def test_the_boot_path_is_the_only_caller_of_the_guarded_restore(self):
        """maybe_restore_on_boot() is safe to call anywhere — it checks that
        memory is empty first — but a second caller would mean a second place
        that reasoning has to hold."""
        import ast
        import pathlib

        callers = []
        for path in list(pathlib.Path("app").rglob("*.py")) + [
            pathlib.Path("server.py"),
            pathlib.Path("worker.py"),
        ]:
            if path.name == "memory_backup.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call):
                    fn = node.func
                    name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                    if name == "maybe_restore_on_boot":
                        callers.append(str(path))

        assert callers == ["server.py"], f"unexpected restore callers: {callers}"


class TestTheCacheModuleIsGone:
    def test_app_core_cache_no_longer_exists(self):
        """Deleted rather than wired. Every repo-metadata read it could have
        served either feeds a guardrail (`archived`) where a stale answer is
        the bug v7.1.1 fixed, or picks a branch to write to (`default_branch`)
        where a stale answer targets the wrong ref. load_config already has an
        in-process TTL cache with a thundering-herd guard, so the one hot read
        that is safe to cache is cached already."""
        with pytest.raises(ImportError):
            import app.core.cache  # noqa: F401

    def test_nothing_references_it(self):
        import pathlib

        for path in list(pathlib.Path("app").rglob("*.py")) + [pathlib.Path("server.py")]:
            assert "core.cache" not in path.read_text(encoding="utf-8"), path


class TestTheGraphHasNoOrphansLeft:
    def test_every_module_is_reachable_or_an_entrypoint(self):
        """The codebase map is only worth having if its findings get acted on.
        Whatever it reports as unreachable must be either genuinely unreachable
        (then delete it) or an entrypoint (then declare it)."""
        graph = json.loads(
            __import__("pathlib").Path("docs/diagrams/codegraph.json").read_text(encoding="utf-8")
        )
        assert graph["stats"]["orphans"] == []
        assert graph["stats"]["cycles"] == []
