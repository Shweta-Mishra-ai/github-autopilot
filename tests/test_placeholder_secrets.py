"""
tests/test_placeholder_secrets.py

The scanner filed issues for keys that say REPLACE_ME.

Run against this repository's own source it produced **eight** findings, every
one a placeholder — including four CRITICAL "private key" hits on
app/security/enhanced_secrets.py itself, where the matched text was the regex
that detects private keys. A scanner that reports its own ruleset as a leak is
not a scanner anyone will read twice.

Three separate causes:

  1. `FALSE_POSITIVE_FILE_PATTERNS` contained `/tests/`, with a leading slash.
     GitHub reports repo-relative paths, so `tests/conftest.py` never matched.
     The exclusion existed and did nothing for the commonest layout there is.
  2. The placeholder word list was five words long and missed `replace`,
     `dummy`, `sample`, `not_real`, `${...}` and `{{ ... }}`.
  3. A PEM header matched on its own. What makes a private key a leak is the
     key MATERIAL, not the header.

The other half of this file matters as much: a scanner tuned until it is quiet
is worthless, so every real credential shape is asserted to still fire.
"""

from __future__ import annotations

import secrets
import string

import pytest

from app.security.enhanced_secrets import scan_diff


def _rand(n: int, alphabet: str = string.ascii_letters + string.digits) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(n))


class TestThisRepositoryScansClean:
    """The regression that started this: run the scanner over its own repo."""

    def test_no_findings_anywhere_in_this_repository(self):
        import pathlib

        offenders = []
        for path in sorted(pathlib.Path(".").rglob("*")):
            if not path.is_file():
                continue
            s = str(path)
            if ".venv" in s or ".git/" in s or "__pycache__" in s:
                continue
            if path.suffix not in (
                ".py", ".md", ".yml", ".yaml", ".json", ".txt", ".cfg", ".toml"
            ) and path.name != ".env.example":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            diff = "\n".join("+" + line for line in text.splitlines())
            for finding in scan_diff(diff, file_path=s):
                offenders.append(f"{s}:{finding.line_number} {finding.pattern_name}")

        assert offenders == [], (
            "the scanner reports this repository's own source as leaking:\n  "
            + "\n  ".join(offenders)
        )

    def test_the_scanners_own_ruleset_is_not_a_leak(self):
        """The most self-evidently wrong result available: the regex that finds
        private keys, reported as a private key, at CRITICAL."""
        line = '+    ("RSA Private Key", r"-----BEGIN RSA PRIVATE KEY-----", "critical", False),'
        assert scan_diff(line, file_path="app/security/enhanced_secrets.py") == []


class TestPlaceholdersAreNotCredentials:
    @pytest.mark.parametrize(
        "line",
        [
            '+GROQ_API_KEY=gsk_your_key_here',
            '+GITHUB_WEBHOOK_SECRET=REPLACE_WITH_RANDOM_32_CHARS',
            '+MEMORY_BACKUP_KEY=REPLACE_WITH_GENERATED_FERNET_KEY',
            '+api_key = "${GROQ_API_KEY}"',
            '+  api_key: "{{ secrets.GROQ_API_KEY }}"',
            '+api_key = "<your-api-key>"',
            '+api_key = "xxxxxxxxxxxxxxxxxxxxxxxx"',
            '+api_key = "placeholder-value-goes-here"',
            '+api_key = "dummy_key_for_local_dev"',
            '+api_key = "sample-api-key-value-01"',
            '+MCP_API_KEY: "test-mcp-api-key-xyz"',
            '+api_key = "****************"',
        ],
    )
    def test_a_stand_in_value_is_not_reported(self, line):
        assert scan_diff(line, file_path="app/config.py") == []

    def test_a_fixture_pem_whose_body_is_the_word_test(self):
        line = (
            '+"GITHUB_PRIVATE_KEY": "-----BEGIN RSA PRIVATE KEY-----'
            '\\ntest\\n-----END RSA PRIVATE KEY-----"'
        )
        assert scan_diff(line, file_path="app/config.py") == []


class TestPathConventionsAreHonoured:
    """`/tests/` with a leading slash could never match `tests/conftest.py`."""

    @pytest.mark.parametrize(
        "path",
        [
            "tests/conftest.py",
            "tests/test_thing.py",
            "app/handlers/tests/helpers.py",
            "test_module.py",
            "docs/guides/setup.md",
            "doc/setup.md",
            ".env.example",
            "config.yml.template",
            "fixtures/sample_payload.json",
        ],
    )
    def test_convention_paths_are_skipped(self, path):
        real_looking = f'+api_key = "{_rand(40)}"'
        assert scan_diff(real_looking, file_path=path) == []

    def test_application_paths_are_still_scanned(self):
        """The exclusions must not have swallowed the code that matters."""
        real_looking = f'+api_key = "{_rand(40)}"'
        assert scan_diff(real_looking, file_path="app/core/config.py")


class TestRealCredentialsStillFire:
    """A scanner tuned until it is silent has been broken, not fixed."""

    @pytest.mark.parametrize(
        "name,line",
        [
            ("aws", '+AWS_ACCESS_KEY_ID = "AKIA{}"'),
            ("groq", '+GROQ_API_KEY = "gsk_{}"'),
            ("github_pat", '+token = "ghp_{}"'),
            ("stripe", '+stripe = "sk_live_{}"'),
            ("generic", '+api_key = "{}"'),
        ],
    )
    def test_a_real_looking_credential_in_app_code_is_reported(self, name, line):
        filler = {
            "aws": _rand(16, string.ascii_uppercase + string.digits),
            "groq": _rand(52),
            "github_pat": _rand(36),
            "stripe": _rand(32),
            "generic": _rand(40),
        }[name]
        assert scan_diff(line.format(filler), file_path="app/config.py"), name

    def test_a_multi_line_private_key_is_reported(self):
        """The common shape of a real leak: header on its own line, base64
        body on the lines after it. Requiring body on the SAME line would have
        missed every one of these."""
        diff = (
            "+-----BEGIN RSA PRIVATE KEY-----\n"
            f"+{_rand(64)}\n+{_rand(64)}\n"
            "+-----END RSA PRIVATE KEY-----"
        )
        assert scan_diff(diff, file_path="app/key.py")

    def test_a_private_key_embedded_with_escaped_newlines_is_reported(self):
        line = f'+KEY = "-----BEGIN RSA PRIVATE KEY-----\\n{_rand(64)}\\n-----END RSA PRIVATE KEY-----"'
        assert scan_diff(line, file_path="app/key.py")

    @pytest.mark.parametrize("header", ["RSA PRIVATE KEY", "EC PRIVATE KEY", "PRIVATE KEY"])
    def test_every_pem_variant_still_needs_material_and_still_fires(self, header):
        bare = f"+-----BEGIN {header}-----"
        assert scan_diff(bare, file_path="app/key.py") == []

        with_body = f"+-----BEGIN {header}-----\n+{_rand(64)}"
        assert scan_diff(with_body, file_path="app/key.py")

    def test_detection_is_statistically_reliable_not_lucky(self):
        """Generated keys are random, so detection is a distribution, not a
        yes/no. Forty samples rather than one seed that happened to work."""
        caught = sum(
            1 for _ in range(40) if scan_diff(f'+api_key = "{_rand(40)}"', file_path="app/c.py")
        )
        assert caught >= 38, f"only {caught}/40 random credentials detected"


class TestPlaceholderWordsNeverSuppressARealSecret:
    """
    Placeholder matching was a case-insensitive SUBSTRING check, and English
    words are short. A genuine GitHub token whose random tail happened to
    contain "fake" was silently dropped:

        ghp_zHDiR5GWjhyRy5AGMUwGLGBfzZ8DJzpGFAKe
                                          ^^^^ -> "fake"

    Measured at 1 in 30,000 — rare enough to pass review, common enough that
    CI found it within a day, and in the one direction a secret scanner must
    never fail. Two fixes, and this class covers both.
    """

    REAL_TOKEN_CI_MISSED = "ghp_zHDiR5GWjhyRy5AGMUwGLGBfzZ8DJzpGFAKe"

    def test_the_exact_token_ci_caught(self):
        assert scan_diff(f'+t = "{self.REAL_TOKEN_CI_MISSED}"', file_path="app/x.py")

    @pytest.mark.parametrize(
        "token",
        [
            "ghp_aaaaaaaaaaaaaaaaaaFAKEaaaaaaaaaaaaaa",
            "ghp_bbbbbbbbbbbbbbbTODObbbbbbbbbbbbbbbbb",
            "ghp_ccccccccccccccDUMMYcccccccccccccccc1",
            "ghp_dddddddddddSAMPLEddddddddddddddddd12",
            "ghp_eeeeeeeeeeeEXAMPLEeeeeeeeeeeeeeeee12",
        ],
    )
    def test_a_placeholder_word_inside_a_random_tail_is_a_coincidence(self, token):
        """Whole words only. A provider's tail is one long token, so a word
        buried in it is never a word."""
        assert scan_diff(f'+t = "{token}"', file_path="app/x.py"), token

    def test_a_provider_prefix_outranks_every_word_heuristic(self):
        """`ghp_` is issued by GitHub and nobody else. That prefix IS the
        context, so word guessing can only lose information there."""
        assert scan_diff('+t = "ghp_fakefakefakefakefakefakefakefakefake"', file_path="app/x.py")

    def test_shape_rules_still_apply_to_anchored_patterns(self):
        """The exemption is for WORD rules. A token of repeated characters is
        unambiguous whatever prefix it wears."""
        assert scan_diff('+t = "ghp_' + "X" * 36 + '"', file_path="app/x.py") == []

    def test_weak_context_patterns_keep_their_word_rules(self):
        """`api_key = "..."` could be anything, so there the words are the only
        signal available and must keep working."""
        assert scan_diff('+api_key = "replace_with_your_real_key_here"', file_path="app/x.py") == []
        assert scan_diff('+api_key = "dummy_value_for_tests"', file_path="app/x.py") == []

    def test_detection_holds_across_many_random_tokens(self):
        """The original bug showed up once in 30,000, so a handful of samples
        would not have caught it. This is sized to notice a regression that
        reintroduces substring matching, which would fail at roughly 1 in 750
        here."""
        import secrets
        import string

        alnum = string.ascii_letters + string.digits
        misses = 0
        for _ in range(3000):
            token = "ghp_" + "".join(secrets.choice(alnum) for _ in range(36))
            if not scan_diff(f'+t = "{token}"', file_path="app/x.py"):
                misses += 1
        assert misses == 0, f"{misses}/3000 real tokens missed"
