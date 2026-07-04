"""tests/test_scanner.py — app/security/scanner.py (GitHub Security APIs reader)."""

from unittest.mock import patch

from app.security.scanner import (
    SecurityFinding,
    SecurityReport,
    run_pr_security_scan,
    run_security_scan,
)


class TestSecurityFinding:
    def test_severity_rank_orders_correctly(self):
        assert SecurityFinding("s", "critical", "t", "d").severity_rank == 4
        assert SecurityFinding("s", "high", "t", "d").severity_rank == 3
        assert SecurityFinding("s", "medium", "t", "d").severity_rank == 2
        assert SecurityFinding("s", "low", "t", "d").severity_rank == 1
        assert SecurityFinding("s", "unknown", "t", "d").severity_rank == 0


class TestSecurityReport:
    def _finding(self, severity, **kw):
        return SecurityFinding(source="x", severity=severity, title="t", description="d", **kw)

    def test_all_findings_sorted_by_severity_desc(self):
        report = SecurityReport(repo="o/r")
        report.dependabot = [self._finding("low")]
        report.codeql = [self._finding("critical")]
        report.secrets = [self._finding("medium")]
        ranks = [f.severity_rank for f in report.all_findings]
        assert ranks == sorted(ranks, reverse=True)

    def test_counts(self):
        report = SecurityReport(repo="o/r")
        report.dependabot = [self._finding("critical"), self._finding("high")]
        report.codeql = [self._finding("high")]
        assert report.critical_count == 1
        assert report.high_count == 2
        assert report.total_count == 3

    def test_to_markdown_all_clear(self):
        report = SecurityReport(repo="o/r")
        md = report.to_markdown()
        assert "All Clear" in md
        assert "o/r" in md

    def test_to_markdown_with_findings_and_severity_summary(self):
        report = SecurityReport(repo="o/r")
        report.dependabot = [self._finding("critical", package="lodash", cve_id="CVE-1", url="u")]
        report.codeql = [self._finding("high", file_path="app.py", line_number=10)]
        report.secrets = [self._finding("critical")]
        md = report.to_markdown()
        assert "3 finding(s)" in md
        assert "critical" in md
        assert "lodash" in md
        assert "app.py:10" in md
        assert "Dependabot Alerts" in md
        assert "CodeQL Findings" in md
        assert "Secret Scanning" in md

    def test_to_markdown_excludes_low_by_default(self):
        report = SecurityReport(repo="o/r")
        report.dependabot = [self._finding("low", package="foo")]
        md = report.to_markdown()
        assert "foo" not in md

    def test_to_markdown_includes_low_when_requested(self):
        report = SecurityReport(repo="o/r")
        report.dependabot = [self._finding("low", package="foo")]
        md = report.to_markdown(include_low=True)
        assert "foo" in md

    def test_to_markdown_surfaces_errors(self):
        report = SecurityReport(repo="o/r")
        report.dependabot = [self._finding("high")]
        report.errors = ["CodeQL (not enabled or no permission)"]
        md = report.to_markdown()
        assert "not enabled" in md


class TestRunSecurityScan:
    def test_aggregates_all_three_sources(self):
        dep_alert = {
            "security_advisory": {
                "severity": "high",
                "summary": "vuln",
                "description": "desc",
                "identifiers": [{"type": "CVE", "value": "CVE-2024-1"}],
            },
            "dependency": {"package": {"name": "requests"}},
            "html_url": "https://x",
        }
        codeql_alert = {
            "rule": {"severity": "error", "description": "SQL injection"},
            "most_recent_instance": {"location": {"path": "app.py", "start_line": 5}},
            "message": {"text": "found it"},
            "html_url": "https://y",
        }
        secret_alert = {"secret_type_display_name": "AWS Key", "html_url": "https://z"}

        def fake_gh_get(path, token):
            if "dependabot" in path:
                return [dep_alert]
            if "code-scanning" in path:
                return [codeql_alert]
            if "secret-scanning" in path:
                return [secret_alert]
            return []

        with patch("app.github.client.gh_get", side_effect=fake_gh_get):
            report = run_security_scan("o/r", "tok")

        assert len(report.dependabot) == 1
        assert report.dependabot[0].cve_id == "CVE-2024-1"
        assert len(report.codeql) == 1
        assert report.codeql[0].severity == "high"  # 'error' mapped to 'high'
        assert report.codeql[0].file_path == "app.py"
        assert len(report.secrets) == 1
        assert report.secrets[0].severity == "critical"
        assert report.errors == []

    def test_403_marks_source_unavailable_not_an_error_log(self):
        with patch("app.github.client.gh_get", side_effect=Exception("403 Forbidden")):
            report = run_security_scan("o/r", "tok")
        assert report.dependabot == []
        assert any("Dependabot" in e for e in report.errors)
        assert any("CodeQL" in e for e in report.errors)
        assert any("Secret Scanning" in e for e in report.errors)

    def test_codeql_severity_mapping(self):
        def fake_gh_get(path, token):
            if "code-scanning" in path:
                return [
                    {
                        "rule": {"severity": "warning", "description": "warn finding"},
                        "most_recent_instance": {"location": {}},
                        "message": {},
                        "html_url": "",
                    },
                    {
                        "rule": {"severity": "note", "description": "note finding"},
                        "most_recent_instance": {"location": {}},
                        "message": {},
                        "html_url": "",
                    },
                ]
            return []

        with patch("app.github.client.gh_get", side_effect=fake_gh_get):
            report = run_security_scan("o/r", "tok")
        severities = {f.title: f.severity for f in report.codeql}
        assert severities["warn finding"] == "medium"
        assert severities["note finding"] == "low"


class TestRunPrSecurityScan:
    def test_filters_codeql_to_changed_files(self):
        pr_files = [{"filename": "app.py"}, {"filename": "other.py"}]

        def fake_gh_get(path, token):
            if "pulls" in path and "files" in path:
                return pr_files
            if "code-scanning" in path:
                return [
                    {
                        "rule": {"severity": "error", "description": "in changed file"},
                        "most_recent_instance": {"location": {"path": "app.py", "start_line": 1}},
                        "message": {},
                        "html_url": "",
                    },
                    {
                        "rule": {"severity": "error", "description": "in unchanged file"},
                        "most_recent_instance": {
                            "location": {"path": "unrelated.py", "start_line": 1}
                        },
                        "message": {},
                        "html_url": "",
                    },
                ]
            return []

        with patch("app.github.client.gh_get", side_effect=fake_gh_get):
            report = run_pr_security_scan("o/r", 42, "tok")

        titles = [f.title for f in report.codeql]
        assert "in changed file" in titles
        assert "in unchanged file" not in titles

    def test_pr_files_fetch_failure_falls_back_to_all_codeql(self):
        def fake_gh_get(path, token):
            if "pulls" in path and "files" in path:
                raise Exception("404 not found")
            if "code-scanning" in path:
                return [
                    {
                        "rule": {"severity": "error", "description": "still shown"},
                        "most_recent_instance": {"location": {}},
                        "message": {},
                        "html_url": "",
                    }
                ]
            return []

        with patch("app.github.client.gh_get", side_effect=fake_gh_get):
            report = run_pr_security_scan("o/r", 1, "tok")
        assert len(report.codeql) == 1
