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
    KNOWN_VULNS,
    DepFinding,
    format_dep_findings,
    format_range,
    get_actionable_findings,
    is_vulnerable,
    parse_requirements,
    parse_version,
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

    def test_report_points_at_the_authoritative_source(self):
        """The built-in table is a small fallback; Dependabot is the real one."""
        assert "/secfull" in format_dep_findings([_finding()])

    def test_every_finding_appears_in_the_body(self):
        """Invariant across all severities, including ones added later."""
        findings = [
            _finding(severity=s, package=f"pkg{i}", cve=f"GHSA-{i}{i}{i}{i}")
            for i, s in enumerate(["HIGH", "CRITICAL", "MODERATE", "LOW", "BUILD", "WEIRD"])
        ]
        out = format_dep_findings(findings)
        for f in findings:
            assert f.package in out, f"{f.severity} finding vanished from the report"


class TestVersionParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2.33.0", (2, 33, 0)),
            ("1.2", (1, 2)),
            ("10.0.1", (10, 0, 1)),
            ("2.32.0rc1", (2, 32, 0)),  # pre-release orders below the release
            ("1.2.3.post1", (1, 2, 3)),
            ("1.2.3+local.build", (1, 2, 3)),
            ("  3.1.3  ", (3, 1, 3)),
        ],
    )
    def test_parses_real_version_strings(self, raw, expected):
        assert parse_version(raw) == expected

    @pytest.mark.parametrize("raw", ["", None, "main", "latest", "v-branch", "abc"])
    def test_unparseable_returns_none(self, raw):
        assert parse_version(raw) is None

    def test_numeric_not_lexicographic_ordering(self):
        """The classic version-compare bug: "9" > "10" as strings."""
        assert parse_version("2.9.0") < parse_version("2.10.0")


class TestVersionRangeMatching:
    """
    The regression this whole rewrite exists for.

    KNOWN_VULNS used re.match on a regex prefix. `r"2\\.3"` is a PREFIX test, so
    it matched requests 2.30 through 2.39 — including 2.33.0, the version this
    project pins — for an advisory that only affects the 2.3.x series.
    """

    @pytest.mark.parametrize("version", ["2.30.0", "2.31.0", "2.32.0", "2.33.0", "2.39.9"])
    def test_two_dot_thirty_something_is_not_two_dot_three_x(self, version):
        assert is_vulnerable(version, "2.3.0", "2.4.0") is False

    @pytest.mark.parametrize("version", ["2.3.0", "2.3.1", "2.3.9"])
    def test_the_actual_affected_series_still_matches(self, version):
        assert is_vulnerable(version, "2.3.0", "2.4.0") is True

    def test_lower_bound_is_inclusive(self):
        assert is_vulnerable("3.0.0", "3.0.0", "4.0.0") is True

    def test_upper_bound_is_exclusive(self):
        """The fixed release itself is not vulnerable."""
        assert is_vulnerable("4.0.0", "3.0.0", "4.0.0") is False

    def test_below_the_range_is_clean(self):
        assert is_vulnerable("2.9.9", "3.0.0", "4.0.0") is False

    def test_open_lower_bound(self):
        assert is_vulnerable("0.0.1", None, "2.0.0") is True

    def test_open_upper_bound_means_no_fix_yet(self):
        assert is_vulnerable("99.0.0", "1.0.0", None) is True

    def test_unequal_length_versions_compare_correctly(self):
        """2.32 and 2.32.0 are the same release."""
        assert is_vulnerable("2.32", "2.3.0", "2.4.0") is False
        assert is_vulnerable("3.1", "3.0.0", "4.0.0") is True

    def test_unparseable_version_is_never_reported(self):
        """Never assert a vulnerability we cannot substantiate."""
        assert is_vulnerable("main", None, None) is False
        assert is_vulnerable("", "1.0.0", "2.0.0") is False


class TestNoFalsePositivesOnThisRepo:
    def _requirements(self):
        with open("requirements.txt", encoding="utf-8") as fh:
            return fh.read()

    def test_pinned_requests_is_not_flagged(self):
        """requests==2.33.0 was reported vulnerable for a 2.3.x advisory."""
        flagged = {f.package for f in scan_requirements_txt(self._requirements())}
        assert "requests" not in flagged

    def test_pinned_cryptography_is_not_flagged(self):
        """cryptography==50.0.0 builds fine; the constraint is 46.x only."""
        flagged = {f.package for f in scan_requirements_txt(self._requirements())}
        assert "cryptography" not in flagged

    def test_nothing_actionable_in_this_repo(self):
        """A HIGH/CRITICAL finding here would open a GitHub issue on every push."""
        actionable = get_actionable_findings(scan_requirements_txt(self._requirements()))
        assert actionable == [], f"would file an issue for: {actionable}"


class TestRangeTableIntegrity:
    def test_every_entry_has_six_fields(self):
        for entry in KNOWN_VULNS:
            assert len(entry) == 6, f"malformed entry: {entry}"

    def test_every_bound_is_parseable(self):
        """An unparseable bound silently disables the check."""
        for pkg, lo, hi, *_ in KNOWN_VULNS:
            if lo is not None:
                assert parse_version(lo) is not None, f"{pkg}: bad lower bound {lo!r}"
            if hi is not None:
                assert parse_version(hi) is not None, f"{pkg}: bad upper bound {hi!r}"

    def test_no_range_is_inverted(self):
        for pkg, lo, hi, *_ in KNOWN_VULNS:
            if lo and hi:
                assert parse_version(lo) < parse_version(hi), f"{pkg}: {lo} >= {hi}"

    def test_no_range_is_unbounded_above_without_reason(self):
        """An open upper bound claims every future release is affected. Allowed,
        but it must be deliberate — this pins the current count so adding one
        is a conscious edit."""
        open_ended = [(p, lo) for p, lo, hi, *_ in KNOWN_VULNS if hi is None]
        assert open_ended == [], f"unbounded ranges will flag future releases: {open_ended}"

    def test_severities_are_known(self):
        valid = {"LOW", "MODERATE", "HIGH", "CRITICAL", "BUILD"}
        for pkg, _lo, _hi, sev, *_ in KNOWN_VULNS:
            assert sev in valid, f"{pkg}: unknown severity {sev!r}"


class TestRangeFormatting:
    def test_both_bounds(self):
        assert format_range("3.0.0", "4.0.0") == ">=3.0.0,<4.0.0"

    def test_upper_only(self):
        assert format_range(None, "2.0.0") == "<2.0.0"

    def test_lower_only(self):
        assert format_range("1.0.0", None) == ">=1.0.0"

    def test_finding_carries_the_range_it_matched(self):
        f = scan_requirements_txt("flask==3.1.3")[0]
        assert f.affected_range == ">=3.0.0,<4.0.0"

    def test_report_shows_the_range_and_the_pinned_version(self):
        out = format_dep_findings(scan_requirements_txt("flask==3.1.3"))
        assert "flask>=3.0.0,<4.0.0" in out
        assert "you have `3.1.3`" in out

    def test_build_findings_are_rendered_not_silently_dropped(self):
        """BUILD entries were counted in the header and rendered by no section,
        so the reader saw "3 packages" above a list of two."""
        out = format_dep_findings(scan_requirements_txt("cryptography==46.0.1"))
        assert "RENDER-001" in out
        assert "Build Constraints" in out

    def test_build_findings_are_excluded_from_the_advisory_count(self):
        out = format_dep_findings(scan_requirements_txt("cryptography==46.0.1"))
        assert "1 advisory across 1 package(s)." in out
