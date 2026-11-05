"""
tests/test_dependencies.py

The dependency scan asks GitHub's advisory database about the exact pinned
versions. It used to match a hand-written table of ten advisories for three
packages and tell every other dependency it was not vulnerable.
"""

from unittest.mock import MagicMock

import pytest

from app.security.dependencies import (
    ISSUE_SEVERITIES,
    AdvisoriesUnavailable,
    DepFinding,
    format_dep_findings,
    get_actionable_findings,
    parse_requirements,
    scan_requirements_txt,
    unpinned_requirements,
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


class TestActionability:
    @pytest.mark.parametrize("severity", sorted(ISSUE_SEVERITIES))
    def test_high_severity_unaccepted_is_actionable(self, severity):
        assert _finding(severity=severity).is_actionable is True

    @pytest.mark.parametrize("severity", ["LOW", "MODERATE"])
    def test_low_severity_is_not_actionable(self, severity):
        assert _finding(severity=severity).is_actionable is False

    def test_there_is_no_built_in_acceptance_list(self):
        """It held this project's own "LOW risk for our use" decisions and
        applied them to every repository."""
        import app.security.dependencies as deps

        assert not hasattr(deps, "ACCEPTED_CVES")

    def test_filter_keeps_only_actionable(self):
        findings = [
            _finding(severity="HIGH", cve="GHSA-aaaa-aaaa-aaaa"),
            _finding(severity="LOW", cve="GHSA-bbbb-bbbb-bbbb"),
            _finding(severity="MODERATE", cve="GHSA-cccc-cccc-cccc"),
        ]
        assert len(get_actionable_findings(findings)) == 1

    def test_filter_on_empty_input(self):
        assert get_actionable_findings([]) == []


class TestFormatting:
    def test_no_findings_renders_nothing(self):
        assert format_dep_findings([]) == ""

    def test_header_states_the_count(self):
        out = format_dep_findings(
            [_finding(package="a"), _finding(package="b"), _finding(package="b")]
        )
        assert "3 advisories across 2 package(s)." in out

    def test_header_is_singular_for_one_advisory(self):
        assert "1 advisory across 1 package(s)." in format_dep_findings([_finding()])

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

    def test_unknown_severity_is_rendered_not_silently_dropped(self):
        """The severity buckets are an allow-list, so anything unanticipated
        used to be counted in the header and then rendered by no section — a
        security report quietly omitting a finding."""
        out = format_dep_findings([_finding(severity="WEIRD", package="mystery")])
        assert "mystery" in out
        assert "1 advisory across 1 package(s)." in out

    def test_report_names_its_source(self):
        assert "GitHub's reviewed advisory database" in format_dep_findings([_finding()])

    def test_every_finding_appears_in_the_body(self):
        """Invariant across all severities, including ones added later."""
        findings = [
            _finding(severity=s, package=f"pkg{i}", cve=f"GHSA-{i}{i}{i}{i}")
            for i, s in enumerate(["HIGH", "CRITICAL", "MODERATE", "LOW", "WEIRD"])
        ]
        out = format_dep_findings(findings)
        for f in findings:
            assert f.package in out, f"{f.severity} finding vanished from the report"


def _advisory(ghsa, pkg, rng, patched, severity="high", summary="s", withdrawn=None):
    return {
        "ghsa_id": ghsa, "cve_id": None, "summary": summary, "severity": severity,
        "withdrawn_at": withdrawn,
        "vulnerabilities": [{
            "package": {"ecosystem": "pip", "name": pkg},
            "vulnerable_version_range": rng, "first_patched_version": patched,
        }],
    }


class TestAdvisoryScan:
    REQS = "requests==2.3.0\nFlask==3.0.0\nnumpy>=1.0\n# c\n-r other.txt\n"

    def test_it_asks_about_exactly_the_pinned_versions(self):
        get = MagicMock(return_value=[])
        assert scan_requirements_txt(self.REQS, "tok", get=get) == []
        path = get.call_args[0][0]
        assert path.startswith("/advisories?ecosystem=pip")
        assert "affects=requests@2.3.0,flask@3.0.0" in path
        assert "numpy" not in path, "an unpinned package has no version to ask about"

    def test_an_advisory_becomes_a_finding_for_that_package(self):
        get = MagicMock(return_value=[
            _advisory("GHSA-aaaa-bbbb-cccc", "requests", "< 2.4.0", "2.4.0", "medium", "Temp file reuse"),
            _advisory("GHSA-dddd-eeee-ffff", "Flask", ">= 3.0.0, < 3.0.3", "3.0.3", "critical"),
        ])
        found = {f.cve_id: f for f in scan_requirements_txt(self.REQS, "tok", get=get)}
        req = found["GHSA-aaaa-bbbb-cccc"]
        assert (req.package, req.version, req.severity, req.fixed_in) == ("requests", "2.3.0", "MODERATE", "2.4.0")
        assert found["GHSA-dddd-eeee-ffff"].severity == "CRITICAL"
        assert found["GHSA-dddd-eeee-ffff"].is_actionable

    def test_withdrawn_and_unrelated_advisories_are_ignored(self):
        get = MagicMock(return_value=[
            _advisory("GHSA-1111-1111-1111", "requests", "< 2.4.0", "2.4.0", withdrawn="2026-01-01"),
            _advisory("GHSA-2222-2222-2222", "django", "< 5", "5.0"),
        ])
        assert scan_requirements_txt(self.REQS, "tok", get=get) == []

    def test_a_failed_read_is_raised_not_called_clean(self):
        from app.github.client import GitHubError

        with pytest.raises(GitHubError):
            scan_requirements_txt(self.REQS, "tok", get=MagicMock(side_effect=GitHubError("x", 502)))
        with pytest.raises(AdvisoriesUnavailable):
            scan_requirements_txt(self.REQS, "tok", get=MagicMock(return_value={"message": "?"}))

    def test_nothing_pinned_asks_nothing(self):
        get = MagicMock()
        assert scan_requirements_txt("numpy\n", "tok", get=get) == []
        get.assert_not_called()

    def test_unpinned_packages_are_named(self):
        assert unpinned_requirements(self.REQS) == ["numpy"]

    def test_the_report_shows_the_range_and_the_fix(self):
        f = DepFinding("requests", "2.3.0", "MODERATE", "GHSA-x", "d", "< 2.4.0", "2.4.0")
        out = format_dep_findings([f])
        assert "Affected: `< 2.4.0` — you have `2.3.0`; fixed in `2.4.0`" in out
