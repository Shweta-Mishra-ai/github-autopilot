"""
tests/test_partial_files.py

What each consumer of a PR's file list says when the list could not be read,
or is not the whole PR. Each used to say nothing and carry on as though it
had seen every file.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.github.client import GitHubError


class TestACommandWithoutTheDiff:
    def _context(self, error):
        from app.handlers.comments.dispatcher import pr_context

        issue = {"title": "Speed up the cache", "body": "See the diff.", "pull_request": {}}
        with patch("app.github.helpers.pr_files", side_effect=error):
            return pr_context("o/r", 5, issue, "t", MagicMock())

    def test_is_told_the_code_could_not_be_read(self):
        """It returned "" and the caller fell back to the title and body, so
        /explain or /perf answered about code it had never been shown."""
        ctx = self._context(GitHubError("not found", 404))
        assert "Speed up the cache" in ctx
        assert "changed files could not be read (GitHubError)" in ctx
        assert "Do not describe or judge its code" in ctx

    def test_a_passing_failure_is_raised_for_retry(self):
        with pytest.raises(GitHubError):
            self._context(GitHubError("bad gateway", 502))


class TestTheSecurityReportScope:
    def test_says_when_its_alerts_are_not_narrowed_to_the_pr(self):
        from app.security import scanner

        alert = scanner.SecurityFinding(
            source="CodeQL", severity="high", title="SQL injection", description="",
            file_path="app/other.py", line_number=3,
        )
        with patch("app.github.helpers.pr_files", side_effect=GitHubError("bad gateway", 502)), \
             patch.object(scanner, "_scan_dependabot", return_value=[]), \
             patch.object(scanner, "_scan_codeql", return_value=[alert]), \
             patch.object(scanner, "_scan_secrets", return_value=[]):
            report = scanner.run_pr_security_scan("o/r", 5, "t")
        md = report.to_markdown()
        assert "file list could not be read" in md
        assert "whole repository" in md


class TestGitHubsFileCap:
    def test_the_report_says_files_beyond_the_cap_were_not_seen(self):
        from app.github.helpers import PR_FILES_MAX_PAGES, PR_FILES_PER_PAGE
        from app.handlers.pull_request.report import _build_pr_report

        files = [{"filename": f"f{i}", "additions": 1, "deletions": 0}
                 for i in range(PR_FILES_MAX_PAGES * PR_FILES_PER_PAGE)]
        body = _build_pr_report("", "s", "", "", {"number": 1}, files)
        assert "not seen" in body

    def test_a_normal_pr_carries_no_such_note(self):
        from app.handlers.pull_request.report import _build_pr_report

        body = _build_pr_report("", "s", "", "", {"number": 1}, [{"filename": "a"}])
        assert "not seen" not in body
