"""Audit round 2: licence and requirement parsing gaps."""

from unittest.mock import patch

from app.security.dependencies import parse_requirements, requirement_names, scan_requirements_txt
from app.security.licenses import classify_expression, format_findings, scan_requirements_report


class TestOrLaterIsPartOfTheName:
    """Split on "or", "GPLv2 or later" became a choice between GPLv2 and an
    unknown licence called "later" — and the permissive-most branch won."""

    def test_trove_lgpl_or_later_is_copyleft(self):
        assert classify_expression("GNU Lesser General Public License v3 or later (LGPLv3+)") == "copyleft"

    def test_library_or_lesser_is_copyleft(self):
        assert classify_expression("GNU Library or Lesser General Public License (LGPL)") == "copyleft"

    def test_free_text_gpl_or_later_is_copyleft(self):
        assert classify_expression("GPLv2 or later") == "copyleft"

    def test_a_real_choice_still_takes_the_permissive_branch(self):
        assert classify_expression("MIT or GPL-3.0") == "safe"


class TestPinsTheScannerUsedToSkip:
    def test_spaces_around_the_operator(self):
        assert parse_requirements("flask == 3.0.0\n") == [{"name": "flask", "version": "3.0.0"}]

    def test_a_spaced_pin_is_scanned_for_cves(self):
        assert scan_requirements_txt("flask == 3.0.0\n")

    def test_dotted_names_and_markers(self):
        out = parse_requirements('zope.interface==6.0 ; python_version<"3.9"\n')
        assert out == [{"name": "zope-interface", "version": "6.0"}]

    def test_requirement_names_include_unpinned_lines(self):
        assert requirement_names("flask>=3\n-r base.txt\nrequests\ngit+https://x/y.git\n") == [
            "flask",
            "requests",
        ]


class TestCoverageIsReported:
    def test_unchecked_packages_are_not_reported_as_all_permissive(self):
        def lookup(name):
            risk = "unchecked" if name == "private-pkg" else "safe"
            return {"package": name, "license": "MIT", "risk": risk}

        with patch("app.security.licenses.check_package_license", side_effect=lookup):
            report = scan_requirements_report("flask>=3\nprivate-pkg==1.0\n")
        assert report == {"findings": [], "total": 2, "checked": 1}
        text = format_findings(report["findings"], report["total"], report["checked"])
        assert "partial" in text and "1 of 2" in text
        assert "All checked" not in text

    def test_packages_past_the_limit_count_as_unchecked(self):
        reqs = "".join(f"p{i}==1\n" for i in range(25))
        with patch(
            "app.security.licenses.check_package_license",
            side_effect=lambda n: {"package": n, "license": "MIT", "risk": "safe"},
        ):
            report = scan_requirements_report(reqs)
        assert report["total"] == 25 and report["checked"] == 20
