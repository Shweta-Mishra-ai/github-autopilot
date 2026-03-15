"""
Tests - tests/test_secrets.py
V3: Unit tests for secret detection scanner.
"""

import pytest
from app.security.secrets import scan_diff, _entropy, SecretFinding


class TestSecretDetection:

    def test_detects_github_token(self):
        diff = "+token = 'ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ123456789'"
        findings = scan_diff(diff)
        assert len(findings) > 0
        assert any(f.pattern_name == "GitHub Token" for f in findings)

    def test_detects_aws_access_key(self):
        diff = "+aws_key = 'AKIAIOSFODNN7EXAMPLE'"
        findings = scan_diff(diff)
        assert len(findings) > 0
        assert any(f.pattern_name == "AWS Access Key" for f in findings)

    def test_detects_private_key(self):
        diff = "+-----BEGIN RSA PRIVATE KEY-----"
        findings = scan_diff(diff)
        assert len(findings) > 0
        assert any(f.pattern_name == "Private Key" for f in findings)

    def test_ignores_deleted_lines(self):
        diff = "-token = 'ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ123456789'"
        findings = scan_diff(diff)
        assert len(findings) == 0

    def test_ignores_context_lines(self):
        diff = " token = 'ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ123456789'"
        findings = scan_diff(diff)
        assert len(findings) == 0

    def test_clean_diff_returns_empty(self):
        diff = "+def hello():\n+    return 'world'"
        findings = scan_diff(diff)
        assert findings == []

    def test_finding_has_correct_fields(self):
        diff = "+api_key = 'ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ123456789'"
        findings = scan_diff(diff)
        assert len(findings) > 0
        f = findings[0]
        assert hasattr(f, "pattern_name")
        assert hasattr(f, "line_number")
        assert hasattr(f, "severity")
        assert hasattr(f, "redacted_match")

    def test_redacted_match_hides_secret(self):
        diff = "+token = 'ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ123456789'"
        findings = scan_diff(diff)
        for f in findings:
            assert "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ123456789" not in f.redacted_match

    def test_multiple_secrets_in_diff(self):
        diff = (
            "+aws = 'AKIAIOSFODNN7EXAMPLE'\n"
            "+token = 'ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ123456789'"
        )
        findings = scan_diff(diff)
        assert len(findings) >= 2

    def test_empty_diff_returns_empty(self):
        assert scan_diff("") == []

    def test_entropy_high_for_random_string(self):
        random_str = "aB3kP9mXq2nR7sT1vY5wZ8"
        assert _entropy(random_str) > 3.5

    def test_entropy_low_for_simple_string(self):
        simple = "aaaaaaaaaaaaaaaa"
        assert _entropy(simple) < 1.0

    def test_entropy_empty_string(self):
        assert _entropy("") == 0.0

    def test_groq_api_key_detected(self):
        long_key = "gsk_" + "a" * 52
        diff = f"+GROQ_API_KEY = '{long_key}'"
        findings = scan_diff(diff)
        assert len(findings) > 0

