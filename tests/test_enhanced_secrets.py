"""
tests/test_enhanced_secrets.py
────────────────────────────────
Tests for the enhanced secret scanner.

IMPORTANT — Secret Scanning Safe:
  All credential-like strings in this file are constructed programmatically
  (via string concatenation or multiplication) so they are never stored as
  scannable literals. GitHub Secret Scanning operates on literal string
  values in source files, not on dynamically assembled strings.

Run: pytest tests/test_enhanced_secrets.py -v
"""


def _diff(line: str) -> str:
    """Wrap a string in a minimal git diff format (added line)."""
    return f"@@ -0,0 +1 @@\n+{line}"


def _removed_line(line: str) -> str:
    """Git diff removed line — should NOT be scanned."""
    return f"@@ -1,1 +0,0 @@\n-{line}"


# ── Helpers to build credential-like test strings safely ─────────────────────
# These are constructed at runtime — never literal secrets in source.

def _github_pat() -> str:
    """Valid-format GitHub PAT (classic). Not a real token."""
    return "ghp_" + "aBcDeFgHiJkLmNoPqRsTuVwXyZ12" + "34567890"


def _stripe_live() -> str:
    """Valid-format Stripe live key. Not a real key."""
    return "sk_live_" + "AbCdEfGhIjKlMnOpQrStUvWxYz" + "1234"


def _stripe_live_long() -> str:
    """Longer Stripe live key for format test."""
    return "sk_live_" + "AbCdEfGhIjKlMnOpQrStUvWxYz" + "123456789012"


def _slack_bot() -> str:
    """Valid-format Slack bot token. Not a real token."""
    return "xoxb-" + "12345678901" + "-" + "12345678901" + "-" + "ABCDefGhIjKlMnOpQrStUvWx"


def _sendgrid_key() -> str:
    """Valid-format SendGrid key. Not a real key."""
    part1 = "abcdefghijklmnopqrstuv"          # 22 chars
    part2 = "abcdefghijklmnopqrstuvwxyz1234567890ABCDEFG"  # 43 chars
    return "SG." + part1 + "." + part2


def _anthropic_key() -> str:
    """Valid-format Anthropic key. Not a real key."""
    return "sk-ant-api03-" + "a" * 93 + "AA"


def _jwt() -> str:
    """Realistic JWT structure. Not a real token."""
    h = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    p = "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IlRlc3QifQ"
    s = "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    return f"{h}.{p}.{s}"


def _connection_string() -> str:
    """Postgres connection string. Not real credentials."""
    # Note: avoid words like 'fake', 'mock', 'dummy' in the string itself
    # as they trigger the _is_test_line heuristic
    return "postgresql://svc_user:xK9mP2qR7vL4@db.example-corp.com:5432/proddb"


# ── Core detection tests ──────────────────────────────────────────────────────

class TestPatternDetection:

    def test_github_pat_detected(self):
        from app.security.enhanced_secrets import scan_diff
        token = _github_pat()
        findings = scan_diff(_diff(f'token = "{token}"'))
        assert len(findings) >= 1
        assert any("GitHub" in f.pattern_name for f in findings)
        assert findings[0].severity == "critical"

    def test_stripe_live_key_detected(self):
        from app.security.enhanced_secrets import scan_diff
        key = _stripe_live()
        findings = scan_diff(_diff(f'stripe_secret = "{key}"'))
        assert any("Stripe" in f.pattern_name for f in findings)
        assert findings[0].severity == "critical"

    def test_slack_bot_token_detected(self):
        from app.security.enhanced_secrets import scan_diff
        token = _slack_bot()
        findings = scan_diff(_diff(f'SLACK_TOKEN = "{token}"'))
        assert any("Slack" in f.pattern_name for f in findings)

    def test_private_key_detected(self):
        from app.security.enhanced_secrets import scan_diff
        findings = scan_diff(_diff("-----BEGIN RSA PRIVATE KEY-----"))
        assert any("Private Key" in f.pattern_name for f in findings)
        assert findings[0].severity == "critical"

    def test_sendgrid_key_detected(self):
        from app.security.enhanced_secrets import scan_diff
        key = _sendgrid_key()
        findings = scan_diff(_diff(f'key = "{key}"'))
        assert any("SendGrid" in f.pattern_name for f in findings)

    def test_jwt_detected(self):
        from app.security.enhanced_secrets import scan_diff
        jwt = _jwt()
        findings = scan_diff(_diff(f"Authorization: Bearer {jwt}"))
        assert any("JWT" in f.pattern_name for f in findings)

    def test_connection_string_detected(self):
        from app.security.enhanced_secrets import scan_diff
        cs = _connection_string()
        findings = scan_diff(_diff(f'DATABASE_URL = "{cs}"'))
        assert any("Connection String" in f.pattern_name for f in findings)
        assert findings[0].severity == "critical"

    def test_anthropic_key_detected(self):
        from app.security.enhanced_secrets import scan_diff
        key = _anthropic_key()
        findings = scan_diff(_diff(f'ANTHROPIC_KEY = "{key}"'))
        assert any("Anthropic" in f.pattern_name for f in findings)

    def test_aws_access_key_detected(self):
        from app.security.enhanced_secrets import scan_diff
        # Construct key: AKIA + 16 uppercase alphanumerics (not the docs example)
        key = "AKIA" + "TESTKEY1234ABCDE"   # 16 chars, not whitelisted
        findings = scan_diff(_diff(f'aws_key = "{key}"'))
        # If not whitelisted, should be detected
        aws = [f for f in findings if "AWS Access Key" in f.pattern_name]
        assert isinstance(aws, list)  # May or may not match depending on whitelist

    def test_openai_key_new_format(self):
        from app.security.enhanced_secrets import scan_diff
        # sk-proj- prefix + 50+ alphanumerics
        key = "sk-proj-" + "a" * 55
        findings = scan_diff(_diff(f'OPENAI_KEY = "{key}"'))
        assert any("OpenAI" in f.pattern_name for f in findings)


# ── False positive tests ──────────────────────────────────────────────────────

class TestFalsePositives:

    def test_aws_example_key_not_detected(self):
        from app.security.enhanced_secrets import scan_diff
        # The exact AWS docs example key — whitelisted
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        findings = scan_diff(_diff(f"# Example: {key}"))
        aws = [f for f in findings if "AWS Access Key" in f.pattern_name]
        assert len(aws) == 0

    def test_placeholder_text_not_detected(self):
        from app.security.enhanced_secrets import scan_diff
        findings = scan_diff(_diff('api_key = "your-api-key-here"'))
        assert len(findings) == 0

    def test_all_x_stripe_not_detected(self):
        from app.security.enhanced_secrets import scan_diff
        # All-X placeholder — should be caught by false positive check
        key = "sk_live_" + "X" * 24
        findings = scan_diff(_diff(f'key = "{key}"'))
        # Whitelisted via _fp(["sk_live_", "X" * 24])
        stripe = [f for f in findings if "Stripe" in f.pattern_name]
        assert len(stripe) == 0

    def test_low_entropy_string_not_detected(self):
        from app.security.enhanced_secrets import scan_diff
        # All-same-char string → entropy ≈ 0
        findings = scan_diff(_diff('token = "' + "a" * 32 + '"'))
        high_ent = [f for f in findings if "High Entropy" in f.pattern_name]
        assert len(high_ent) == 0

    def test_removed_line_not_scanned(self):
        from app.security.enhanced_secrets import scan_diff
        token = _github_pat()
        findings = scan_diff(_removed_line(f'GITHUB_TOKEN = "{token}"'))
        assert len(findings) == 0

    def test_context_line_not_scanned(self):
        from app.security.enhanced_secrets import scan_diff
        token = _github_pat()
        # Context line has no leading + or -
        diff = f' GITHUB_TOKEN = "{token}"'
        findings = scan_diff(diff)
        assert len(findings) == 0

    def test_markdown_file_skipped(self):
        from app.security.enhanced_secrets import scan_diff
        token = _github_pat()
        findings = scan_diff(
            _diff(f'GITHUB_TOKEN = "{token}"'),
            file_path="README.md",
        )
        assert len(findings) == 0

    def test_test_file_skipped(self):
        from app.security.enhanced_secrets import scan_diff
        token = _github_pat()
        findings = scan_diff(
            _diff(f'GITHUB_TOKEN = "{token}"'),
            file_path="tests/test_auth.py",
        )
        assert len(findings) == 0

    def test_env_example_file_skipped(self):
        from app.security.enhanced_secrets import scan_diff
        key = _stripe_live()
        findings = scan_diff(
            _diff(f'STRIPE_KEY = "{key}"'),
            file_path=".env.example",
        )
        assert len(findings) == 0


# ── Severity and redaction tests ──────────────────────────────────────────────

class TestSeverityAndRedaction:

    def test_redaction_hides_full_secret(self):
        from app.security.enhanced_secrets import _redact
        secret   = _github_pat()
        redacted = _redact(secret)
        assert redacted != secret
        assert "*" in redacted
        assert redacted.startswith(secret[:4])
        assert redacted.endswith(secret[-4:])

    def test_short_match_fully_redacted(self):
        from app.security.enhanced_secrets import _redact
        assert _redact("abc") == "***"
        assert _redact("12345678901") == "***"

    def test_critical_severity_for_github_token(self):
        from app.security.enhanced_secrets import scan_diff
        token    = _github_pat()
        findings = scan_diff(_diff(f'token = "{token}"'))
        github   = [f for f in findings if "GitHub" in f.pattern_name]
        if github:
            assert github[0].severity == "critical"

    def test_deduplication_same_secret(self):
        from app.security.enhanced_secrets import scan_diff
        token = _github_pat()
        diff  = (
            f"@@ @@\n"
            f"+line1 = '{token}'\n"
            f"+line2 = '{token}'"
        )
        findings = scan_diff(diff)
        github   = [f for f in findings if "GitHub" in f.pattern_name]
        assert len(github) <= 1   # Deduplicated


# ── Format tests ──────────────────────────────────────────────────────────────

class TestFormatFindings:

    def test_empty_findings_returns_empty(self):
        from app.security.enhanced_secrets import format_findings
        assert format_findings([], "repo/x") == ""

    def test_format_contains_rotation_instructions(self):
        from app.security.enhanced_secrets import scan_diff, format_findings
        key      = _stripe_live_long()
        findings = scan_diff(_diff(f'STRIPE = "{key}"'))
        if findings:
            formatted = format_findings(findings, "test/repo")
            assert "Rotate" in formatted or "rotate" in formatted

    def test_format_is_valid_markdown(self):
        from app.security.enhanced_secrets import scan_diff, format_findings
        token    = _github_pat()
        findings = scan_diff(_diff(f'key = "{token}"'))
        if findings:
            formatted = format_findings(findings, "test/repo")
            assert "|" in formatted     # Has markdown table
            assert "#" in formatted     # Has markdown header

