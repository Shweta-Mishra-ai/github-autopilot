"""
tests/test_license_accuracy.py

The licence scanner read `info.license` and nothing else. Six of this
repository's own eight direct dependencies leave that field empty — Flask,
redis, gunicorn, cryptography, PyJWT and structlog all declare their licence in
`license_expression` (PEP 639) or in trove classifiers — so all six were
reported as "unknown", i.e. as something a maintainer must go and check.

A scanner that is wrong about three quarters of a normal requirements file is
worse than no scanner: it teaches people to skim past the section.

The fixtures below are the REAL metadata shapes those packages publish, copied
from live PyPI responses, not shapes invented to make the parser pass.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.security.licenses import (
    check_package_license,
    classify_expression,
    license_from_metadata,
    scan_requirements,
)

# Real PyPI `info` payloads, trimmed to the licence-bearing fields.
REAL_PACKAGES = {
    "flask": {"license_expression": "BSD-3-Clause", "license": "", "classifiers": []},
    "requests": {
        "license": "Apache-2.0",
        "classifiers": ["License :: OSI Approved :: Apache Software License"],
    },
    "redis": {
        "license_expression": "MIT",
        "license": "",
        "classifiers": ["License :: OSI Approved :: MIT License"],
    },
    "gunicorn": {"license_expression": "MIT", "license": "", "classifiers": []},
    "cryptography": {
        "license_expression": "Apache-2.0 OR BSD-3-Clause",
        "license": "",
        "classifiers": [],
    },
    "pyjwt": {"license_expression": "MIT", "license": "", "classifiers": []},
    "python-dotenv": {"license": "BSD-3-Clause", "classifiers": []},
    "structlog": {"license_expression": "MIT OR Apache-2.0", "license": "", "classifiers": []},
}


class TestNoFalsePositivesOnRealDependencies:
    @pytest.mark.parametrize("package", sorted(REAL_PACKAGES))
    def test_this_repos_own_dependencies_are_all_clean(self, package):
        _, risk = license_from_metadata(REAL_PACKAGES[package])
        assert risk == "safe", f"{package} misreported as {risk}"

    def test_an_empty_license_field_is_not_evidence_of_anything(self):
        """The specific bug: six of eight had `license: ""` and were called
        risky on that basis alone."""
        empty = [p for p, i in REAL_PACKAGES.items() if not i.get("license")]
        assert len(empty) >= 5, "fixtures no longer reproduce the original condition"
        for package in empty:
            assert license_from_metadata(REAL_PACKAGES[package])[1] == "safe"

    def test_the_whole_requirements_file_produces_no_findings(self):
        reqs = "\n".join(f"{name}==1.0.0" for name in REAL_PACKAGES)

        def fake(pkg: str):
            display, risk = license_from_metadata(REAL_PACKAGES[pkg])
            return {"package": pkg, "license": display, "risk": risk}

        with patch("app.security.licenses.check_package_license", side_effect=fake):
            assert scan_requirements(reqs) == []


class TestExpressionsAreParsedNotSubstringMatched:
    def test_dual_licence_and_is_copyleft(self):
        """`"MIT" in "MIT AND GPL-3.0"` was True, so a package that genuinely
        imposes the GPL was reported safe — the false negative that actually
        costs someone a licence violation."""
        assert classify_expression("MIT AND GPL-3.0") == "copyleft"

    def test_dual_licence_or_takes_the_permissive_branch(self):
        """With OR the consumer picks, so the permissive branch is the truth."""
        assert classify_expression("MIT OR GPL-3.0") == "safe"
        assert classify_expression("Apache-2.0 OR BSD-3-Clause") == "safe"

    @pytest.mark.parametrize(
        "expr", ["GPL-3.0-or-later", "GPL-3.0+", "GPL-3.0-only", "AGPL-3.0-or-later"]
    )
    def test_spdx_suffixes_do_not_hide_a_copyleft_licence(self, expr):
        assert classify_expression(expr) == "copyleft"

    def test_a_with_exception_clause_keeps_the_base_licence(self):
        assert classify_expression("Apache-2.0 WITH LLVM-exception") == "safe"
        assert classify_expression("GPL-2.0 WITH Classpath-exception-2.0") == "copyleft"

    @pytest.mark.parametrize("name", ["MIT", "MIT License", "mit license", "  MIT  "])
    def test_the_same_licence_written_four_ways_agrees(self, name):
        assert classify_expression(name) == "safe"

    def test_nested_or_inside_and_resolves_conservatively(self):
        assert classify_expression("GPL-3.0 AND (MIT OR Apache-2.0)") == "copyleft"


class TestUncheckableIsNotARisk:
    """A package this scanner could not reach is not evidence of a licence
    problem, and reporting it as one is the false positive that makes the whole
    section ignorable."""

    def _resp(self, status: int, payload=None):
        r = MagicMock()
        r.status_code = status
        r.json.return_value = payload or {}
        return r

    def test_a_package_not_on_pypi_is_unchecked_not_unknown(self):
        with patch("app.security.licenses.requests.get", return_value=self._resp(404)):
            assert check_package_license("my-private-lib")["risk"] == "unchecked"

    def test_a_network_failure_is_unchecked(self):
        with patch("app.security.licenses.requests.get", side_effect=OSError("dns")):
            assert check_package_license("flask")["risk"] == "unchecked"

    def test_unchecked_packages_never_appear_in_the_report(self):
        with patch(
            "app.security.licenses.check_package_license",
            return_value={"package": "x", "license": "not on PyPI", "risk": "unchecked"},
        ):
            assert scan_requirements("x==1.0\ny==2.0\n") == []

    def test_a_genuinely_undeclared_licence_is_still_reported(self):
        """The scanner must not become silent — 'PyPI has this package and it
        declares no licence' is a real finding."""
        with patch(
            "app.security.licenses.requests.get",
            return_value=self._resp(200, {"info": {"license": "", "classifiers": []}}),
        ):
            assert check_package_license("mystery")["risk"] == "unknown"


class TestCopyleftIsStillCaught:
    """The point of removing false positives is to be believed when it fires."""

    @pytest.mark.parametrize(
        "info",
        [
            {"license_expression": "GPL-3.0-or-later"},
            {"license": "GPL-3.0"},
            {"classifiers": ["License :: OSI Approved :: GNU General Public License v3 (GPLv3)"]},
            {"license_expression": "AGPL-3.0"},
            {"license": "Mozilla Public License 2.0 (MPL 2.0)"},
        ],
    )
    def test_copyleft_is_detected_from_every_metadata_source(self, info):
        info.setdefault("classifiers", [])
        assert license_from_metadata(info)[1] == "copyleft"

    def test_a_pasted_licence_text_is_not_treated_as_an_identifier(self):
        """Some packages paste the entire licence body into `license`. Matching
        an identifier inside 3000 words of prose is guesswork."""
        body = "Permission is hereby granted, free of charge, " * 40
        display, risk = license_from_metadata(
            {"license": body, "classifiers": ["License :: OSI Approved :: MIT License"]}
        )
        assert risk == "safe"  # fell through to the classifier, which is reliable
        assert len(display) < 200


class TestParenthesesAreRespected:
    """A naive split on OR cuts "GPL-3.0 AND (MIT OR Apache-2.0)" at the INNER
    operator, yielding "GPL-3.0 AND (MIT" and "Apache-2.0)", then takes the
    permissive branch. The GPL term is mandatory — it sits outside the parens.
    This was a real defect in the first version of the parser, caught by a test
    rather than by reading it."""

    @pytest.mark.parametrize(
        "expr,expected",
        [
            ("GPL-3.0 AND (MIT OR Apache-2.0)", "copyleft"),
            ("(MIT OR Apache-2.0) AND GPL-3.0", "copyleft"),
            ("Apache-2.0 OR (MIT AND GPL-3.0)", "safe"),
            ("(MIT)", "safe"),
            ("((MIT))", "safe"),
            ("(GPL-3.0)", "copyleft"),
        ],
    )
    def test_nesting_resolves_correctly(self, expr, expected):
        assert classify_expression(expr) == expected

    def test_unbalanced_parentheses_do_not_crash(self):
        """Malformed metadata is metadata too."""
        for expr in ["(MIT", "MIT)", "((MIT)", "()"]:
            assert classify_expression(expr) in {"safe", "copyleft", "unknown"}
