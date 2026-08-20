"""
tests/test_dependencies.py

app/security/dependencies.py sat at 42% coverage: the vulnerability matcher and
the whole issue-body formatter were untested, despite deciding what gets filed
as a GitHub issue on every push that touches requirements.txt.
"""

import pytest

from app.security.dependencies import (
    ACCEPTED_CVES,
    ISSUE_SEVERITIES,
    DepFinding,
    format_dep_findings,
    get_actionable_findings,
    parse_requirements,
    scan_requirements_txt,
)


def _finding(severity="HIGH", cve="GHSA-test-0000-0000", package="somepkg"):
    return DepFinding(
        package=package,
        version="1.0.0",
        severity=severity,
        cve_id=cve,
        description="test finding",
    )


class TestParseRequirements:
    def test_parses_a_simple_pin(self):
        assert parse_requirements("flask==3.1.3") == [
            {"name": "flask", "version": "3.1.3"}
        ]

    def test_skips_comments_and_blank_lines(self):
        content = "# a comment\n\nflask==3.1.3\n\n#another\n"
        assert len(parse_requirements(content)) == 1

    def test_strips_extras(self):
        assert parse_requirements("celery[redis]==5.3.6")[0]["name"] == "celery"

    def test_lowercases_package_names(self):
        assert parse_requirements("PyYAML==6.0.2")[0]["name"] == "pyyaml"

    def test_strips_trailing_comment_from_version(self):
        assert parse_requirements("flask==3.1.3  # pinned")[0]["version"] == "3.1.3"

    @pytest.mark.parametrize(
        "line",
        [
            "flask>=3.0.0",  # range, not a pin
            "-r requirements-dev.txt",  # include
            "git+https://github.com/x/y.git",  # VCS
            "flask",  # unpinned
            "   ",
        ],
    )
    def test_skips_unpinnable_lines(self, line):
        assert parse_requirements(line) == []

    def test_empty_input(self):
        assert parse_requirements("") == []

    def test_none_input_does_not_raise(self):
        assert parse_requirements(None) == []


class TestScanRequirements:
    def test_clean_file_yields_no_findings(self):
        assert scan_requirements_txt("boto3==1.34.0\nrich==13.7.0") == []

    def test_known_vulnerable_pin_is_flagged(self):
        findings = scan_requirements_txt("flask==3.1.3")
        assert findings
        assert all(f.package == "flask" for f in findings)

    def test_finding_carries_the_scanned_version(self):
        assert scan_requirements_txt("flask==3.1.3")[0].version == "3.1.3"

    def test_non_matching_version_is_not_flagged(self):
        """The flask rule matches 3.x — a 2.x pin must not trip it."""
        assert scan_requirements_txt("flask==2.0.1") == []

    def test_extras_syntax_is_still_matched(self):
        """Regression: scan_requirements_txt duplicated the parse loop, so a
        parser fix could land in one copy and not the other."""
        assert scan_requirements_txt("flask[async]==3.1.3")

    def test_comments_are_not_scanned(self):
        assert scan_requirements_txt("# flask==3.1.3") == []

    def test_scan_agrees_with_parse_on_what_counts_as_a_package(self):
        content = "flask==3.1.3\n# comment\nrich>=13\ncelery[redis]==5.3.6\n"
        parsed = {p["name"] for p in parse_requirements(content)}
        assert {f.package for f in scan_requirements_txt(content)} <= parsed


class TestActionability:
    @pytest.mark.parametrize("severity", sorted(ISSUE_SEVERITIES))
    def test_high_severity_unaccepted_is_actionable(self, severity):
        assert _finding(severity=severity).is_actionable is True

    @pytest.mark.parametrize("severity", ["LOW", "MODERATE"])
    def test_low_severity_is_not_actionable(self, severity):
        assert _finding(severity=severity).is_actionable is False

    def test_accepted_cve_is_never_actionable(self):
        accepted = next(iter(ACCEPTED_CVES))
        assert _finding(severity="HIGH", cve=accepted).is_actionable is False

    def test_filter_keeps_only_actionable(self):
        findings = [
            _finding(severity="HIGH", cve="GHSA-aaaa-aaaa-aaaa"),
            _finding(severity="LOW", cve="GHSA-bbbb-bbbb-bbbb"),
            _finding(severity="HIGH", cve=next(iter(ACCEPTED_CVES))),
        ]
        assert len(get_actionable_findings(findings)) == 1

    def test_filter_on_empty_input(self):
        assert get_actionable_findings([]) == []


class TestFormatting:
    def test_no_findings_renders_nothing(self):
        assert format_dep_findings([]) == ""

    def test_header_states_the_count(self):
        out = format_dep_findings([_finding(), _finding()])
        assert "2 package(s)" in out

    def test_severity_sections_appear_only_when_populated(self):
        out = format_dep_findings([_finding(severity="HIGH")])
        assert "High Severity" in out
        assert "Moderate Severity" not in out
        assert "Low Severity" not in out

    def test_all_three_sections_render_together(self):
        out = format_dep_findings(
            [
                _finding(severity="HIGH", cve="GHSA-1111-1111-1111"),
                _finding(severity="MODERATE", cve="GHSA-2222-2222-2222"),
                _finding(severity="LOW", cve="GHSA-3333-3333-3333"),
            ]
        )
        assert "High Severity" in out
        assert "Moderate Severity" in out
        assert "Low Severity" in out

    def test_critical_is_grouped_with_high(self):
        out = format_dep_findings([_finding(severity="CRITICAL")])
        assert "High Severity" in out
        assert "🚨" in out

    def test_advisory_link_is_rendered(self):
        out = format_dep_findings([_finding(cve="GHSA-abcd-abcd-abcd")])
        assert "https://github.com/advisories/GHSA-abcd-abcd-abcd" in out

    def test_package_and_version_are_shown(self):
        out = format_dep_findings([_finding(package="flask")])
        assert "`flask==1.0.0`" in out

    def test_unknown_severity_does_not_crash(self):
        out = format_dep_findings([_finding(severity="WEIRD")])
        assert "1 package(s)" in out

    def test_remediation_hint_is_present(self):
        assert "pip install --upgrade" in format_dep_findings([_finding()])
