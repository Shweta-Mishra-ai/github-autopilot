"""
tests/test_comments_security.py

app/handlers/comments/security.py sat at 51% coverage — the entire body of
cmd_security() was untested.

The bug that hid there: the dependency half of the scan read requirements.txt
from the contents API with no `ref`, which serves the repository's *default
branch*. A PR whose only change was adding a vulnerable pin was therefore
scanned against the base file and reported clean.
"""

from unittest.mock import patch

import pytest

from app.handlers.comments import security as S


@pytest.fixture
def gh():
    with patch.object(S, "gh_get") as m:
        yield m


def _pr_issue():
    return {"pull_request": {"url": "https://api.github.com/..."}}


def _route(files, *, head_sha="headsha123", contents_by_ref=None):
    """Route gh_get by path, recording which ref the contents read used."""
    contents_by_ref = contents_by_ref or {}

    def _side_effect(path, token, *a, **kw):
        if path.endswith("/files"):
            return files
        if "/pulls/" in path and "/files" not in path:
            return {"head": {"sha": head_sha}}
        if "/contents/" in path:
            ref = path.split("?ref=")[1] if "?ref=" in path else None
            import base64

            body = contents_by_ref.get(ref, "")
            return {"content": base64.b64encode(body.encode()).decode()}
        raise AssertionError(f"unexpected path {path}")

    return _side_effect


class TestCmdSecurityGuards:
    def test_non_pr_issue_is_declined(self, gh):
        out = S.cmd_security("o/r", 1, {}, "tok")
        assert "works best on Pull Requests" in out
        gh.assert_not_called()

    def test_api_failure_is_reported_not_raised(self, gh):
        gh.side_effect = RuntimeError("github down")
        out = S.cmd_security("o/r", 1, _pr_issue(), "tok")
        assert "Security scan failed" in out


class TestSecretScanning:
    def test_clean_pr_reports_no_secrets(self, gh):
        gh.side_effect = _route([{"filename": "app/a.py", "patch": "+x = 1"}])
        out = S.cmd_security("o/r", 1, _pr_issue(), "tok")
        assert "No secrets detected" in out

    def test_binary_file_with_null_patch_is_skipped(self, gh):
        """A null patch is not an absent key; `.get("patch", "")` returned None
        and the truthiness check was the only thing preventing a crash."""
        gh.side_effect = _route([{"filename": "logo.png", "patch": None}])
        out = S.cmd_security("o/r", 1, _pr_issue(), "tok")
        assert "No secrets detected" in out

    def test_file_without_filename_does_not_raise(self, gh):
        gh.side_effect = _route([{"patch": "+x = 1"}])
        out = S.cmd_security("o/r", 1, _pr_issue(), "tok")
        assert "Security Scan Results" in out

    def test_only_first_ten_files_are_secret_scanned(self, gh):
        from app.security import enhanced_secrets

        files = [{"filename": f"f{i}.py", "patch": "+x"} for i in range(20)]
        gh.side_effect = _route(files)
        # scan_diff is imported inside cmd_security, so patching the module
        # attribute takes effect at call time.
        with patch.object(enhanced_secrets, "scan_diff", return_value=[]) as scan:
            S.cmd_security("o/r", 1, _pr_issue(), "tok")
        assert scan.call_count == 10


class TestDependencyScanUsesPrHead:
    def test_requirements_is_read_at_the_pr_head_not_the_base(self, gh):
        """The regression this file exists for."""
        gh.side_effect = _route(
            [{"filename": "requirements.txt", "patch": "+flask==3.1.3"}],
            head_sha="abc123",
            contents_by_ref={"abc123": "flask==3.1.3"},
        )
        out = S.cmd_security("o/r", 1, _pr_issue(), "tok")

        contents_calls = [
            c.args[0] for c in gh.call_args_list if "/contents/" in c.args[0]
        ]
        assert contents_calls, "requirements.txt was never read"
        assert "?ref=abc123" in contents_calls[0], (
            "dependency scan read the default branch, not the PR head — a PR "
            "adding a vulnerable pin would be reported clean"
        )
        assert "Dependency Findings" in out

    def test_unresolvable_head_falls_back_to_default_branch(self, gh):
        """Losing the ref is better than losing the whole report."""

        def _side_effect(path, token, *a, **kw):
            if path.endswith("/files"):
                return [{"filename": "requirements.txt", "patch": "+x"}]
            if "/pulls/" in path:
                raise RuntimeError("pr fetch failed")
            import base64

            return {"content": base64.b64encode(b"rich==13.7.0").decode()}

        gh.side_effect = _side_effect
        out = S.cmd_security("o/r", 1, _pr_issue(), "tok")
        assert "No vulnerable dependencies" in out

    def test_pr_without_requirements_skips_the_contents_read(self, gh):
        gh.side_effect = _route([{"filename": "app/a.py", "patch": "+x"}])
        S.cmd_security("o/r", 1, _pr_issue(), "tok")
        assert not [c for c in gh.call_args_list if "/contents/" in c.args[0]]

    def test_clean_requirements_reports_no_vulnerabilities(self, gh):
        gh.side_effect = _route(
            [{"filename": "requirements.txt", "patch": "+x"}],
            head_sha="s1",
            contents_by_ref={"s1": "rich==13.7.0"},
        )
        assert "No vulnerable dependencies" in S.cmd_security(
            "o/r", 1, _pr_issue(), "tok"
        )


class TestCmdSecfull:
    def test_returns_report_markdown(self):
        with patch("app.security.scanner.run_security_scan") as run:
            run.return_value.to_markdown.return_value = "# Full Report"
            assert S.cmd_secfull("o/r", "tok") == "# Full Report"

    def test_includes_low_severity(self):
        with patch("app.security.scanner.run_security_scan") as run:
            run.return_value.to_markdown.return_value = "x"
            S.cmd_secfull("o/r", "tok")
            run.return_value.to_markdown.assert_called_once_with(include_low=True)

    def test_failure_is_reported_not_raised(self):
        with patch("app.security.scanner.run_security_scan", side_effect=OSError("x")):
            assert "Security scan failed" in S.cmd_secfull("o/r", "tok")
