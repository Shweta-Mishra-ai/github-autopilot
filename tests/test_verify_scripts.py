"""
The verification scripts must stay runnable and stay truthful.

A script that only ever fails, or that exits 0 whatever happens, is worse
than no script: it trains you to stop reading the output. Both were checked
against a stub server that reproduces this deployment's real failure — a
provider 404 for the configured model — and against a healthy one, so each
was seen to reach both verdicts.
"""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = [ROOT / "scripts/verify.sh", ROOT / "scripts/verify-deployment.sh"]


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
class TestEachScript:
    def test_exists_and_is_executable(self, script):
        assert script.exists(), f"{script.name} is referenced from the docs"
        assert os.access(script, os.X_OK), (
            f"{script.name} is not executable — `git update-index --chmod=+x` it, "
            f"or the documented `./scripts/...` invocation fails"
        )

    def test_is_valid_shell(self, script):
        # bash -n parses without executing. A syntax error in a script nothing
        # imports would otherwise only surface when someone needs it.
        result = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, f"{script.name}: {result.stderr}"

    def test_help_exits_clean_and_prints_no_shell_source(self, script):
        env = {**os.environ, "BASE_URL": "https://example.invalid"}
        result = subprocess.run(
            [str(script), "--help"], capture_output=True, text=True, timeout=60, env=env
        )
        assert result.returncode == 0, f"--help should succeed: {result.stderr}"
        out = result.stdout
        assert script.name in out, "help should name the script"
        # The help prints the header comment block. A hardcoded line range used
        # to run one line long and leak `set -uo pipefail` into the output.
        for leak in ("set -uo pipefail", "#!/usr/bin/env", "FAILED+=("):
            assert leak not in out, f"--help leaked shell source: {leak!r}"

    def test_reports_failure_as_a_nonzero_exit(self, script):
        # Grepping for the shape rather than running the whole thing: a script
        # that prints ✗ and exits 0 is the failure mode that matters, and it
        # cannot be caught by reading the happy path.
        body = script.read_text(encoding="utf-8")
        assert "exit 1" in body, f"{script.name} must exit non-zero when a gate fails"
        assert "exit 0" in body, f"{script.name} must exit zero when everything passes"


class TestVerifyRunsTheSameChecksAsCI:
    """
    The point of the script is that it matches CI. If it drifts it gives
    false confidence, which is worse than not existing — three red builds in
    this repository came from a gate that passes locally under different flags.
    """

    @staticmethod
    def _body() -> str:
        return (ROOT / "scripts/verify.sh").read_text(encoding="utf-8")

    @staticmethod
    def _ci() -> str:
        return (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    def test_it_lints_the_same_paths_with_the_same_rules(self):
        body, ci = self._body(), self._ci()
        assert "ruff check app/" in body, "CI lints app/ only; linting more trains you to ignore it"
        for flag in ("E,F,W,B,C4,SIM", "E501,B008"):
            assert flag in body and flag in ci, f"{flag} must match CI"

    def test_it_checks_the_generated_files_ci_gates_on(self):
        body = self._body()
        assert "app.handlers.readme --check" in body, "a stale README region fails CI"
        assert "app.intelligence.codegraph" in body, "a stale codebase map fails CI"

    def test_it_runs_the_suite_more_than_once_by_default(self):
        # Parts of the suite generate their inputs. A 1-in-30,000 scanner miss
        # and an ordering-dependent failure both reached CI after passing
        # locally exactly once.
        body = self._body()
        assert "RUNS=2" in body, "one green run is weaker evidence than it looks"

    def test_it_does_not_silently_rewrite_the_tree(self):
        body = self._body()
        assert "commit it" in body, (
            "regenerating a file without saying so leaves an uncommitted change "
            "the author never sees"
        )
