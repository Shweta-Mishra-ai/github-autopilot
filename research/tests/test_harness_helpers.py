"""Regression tests for the harness helpers, one per defect found in them.

Each of these was a wrong number before it was a test: a sample outside the
rule's language, a literal search that skipped Stripe and AWS keys, a scanner
scored 0/14 because its line numbers were trusted, a provider match that
split one vendor in two."""
import random
import re

import bench_gitleaks_fulltable as gl
import bench_realworld_multilang as rw
import bench_threetool_expanded as tt
import exrex


class TestGoClassesAreTranslatedBeforeGeneration:
    """exrex drew 'ª', 'À' for \\w and line breaks for \\s: outside Go's
    ASCII classes, so outside the rule's language."""

    def test_classes_inside_and_outside_brackets(self):
        assert gl._ascii_classes(r"[\w-]{3}") == "[A-Za-z0-9_-]{3}"
        assert gl._ascii_classes(r"\w\d") == "[A-Za-z0-9_][0-9]"
        assert gl._ascii_classes(r"a\s") == r"a[ \t]"
        assert gl._ascii_classes(r"\.\-") == r"\.\-"

    def test_generated_values_are_ascii_without_line_breaks(self):
        # \s becomes space or tab; a tab is then rejected by the printable
        # filter and the value redrawn, so only line breaks must never occur
        random.seed(1)
        pat = gl._to_python(r"(?i)[\w-]{40}\s?\d{4}")
        for _ in range(200):
            v = exrex.getone(pat, limit=10)
            assert v.isascii() and "\n" not in v and "\r" not in v, repr(v)

    def test_posix_classes_from_trufflehog(self):
        assert tt._go_to_python("[[:alnum:]_-]+") == "[A-Za-z0-9_-]+"


class TestSecretGroupExtraction:
    def test_first_capture_group_is_the_secret(self):
        rule = {"regex": r"\b(AIza[\w-]{35})(?:['|\"|\n|\r|\s|\x60|;]|$)"}
        assert gl.secret_pattern(rule) == "AIza[A-Za-z0-9_-]{35}"

    def test_no_group_means_the_whole_match(self):
        assert gl.secret_pattern({"regex": r"ghp_[0-9a-zA-Z]{36}"}) == "ghp_[0-9a-zA-Z]{36}"

    def test_keyword_group_of_a_keyword_rule(self):
        rule = {"regex": r"(?i)[\w.-]{0,10}?(?:adafruit)(?:[ \t\w.-]{0,20})=([a-z]{32})"}
        assert gl.keyword_pattern(rule) == "(?:adafruit)"


class TestGitleaksFiltersAreModelled:
    def test_keyword_prefilter(self):
        f = gl.Filters({"allowlist": {}}, {"keywords": ["hooks.slack.com"]})
        assert not f.accepts("x" * 30, 'cfg = "hooksXslackXcom/abc"')
        assert f.accepts("x" * 30, 'cfg = "hooks.slack.com/abc"')

    def test_entropy_threshold_is_strict(self):
        f = gl.Filters({"allowlist": {}}, {"entropy": 1.0})
        assert not f.accepts("ab", "ab")          # entropy exactly 1.0
        assert f.accepts("abc", "abc")


class TestLiteralPrefixesFollowAlternations:
    """The first version stopped at an alternation and so skipped the
    Stripe and AWS types entirely."""

    def test_alternation_then_literal(self):
        assert rw.literal_prefixes(r"(?:sk|rk)_(?:test|live)_[a-z]{10}") == [
            "rk_live_", "rk_test_", "sk_live_", "sk_test_"]

    def test_aws(self):
        assert "AKIA" in rw.literal_prefixes(r"(?:A3T[A-Z0-9]|AKIA|ASIA)[A-Z0-9]{16}")

    def test_no_fixed_start_is_not_distinctive(self):
        assert rw.literal_prefixes(r"[a-f0-9]{32}") == []
        assert rw.literal_prefixes(r"(?:a|b)c") == []       # 'ac' is too short
        assert rw.literal_prefixes(r"(?:a|bcd)efg") == ["aefg", "bcdefg"]


class TestFindingsAreLocatedBySecretNotByLineNumber:
    """TruffleHog reported one token on four lines as four findings on a
    fifth line; trusting the number scored it 0/14."""

    def test_four_findings_land_on_four_lines(self, tmp_path):
        tok = "glpat-" + "A1b2C3d4E5f6G7h8I9j0"
        (tmp_path / "doc.rst").write_text(
            "intro\n" + "\n".join(f'gl = "{tok}"' if i in (3, 6, 11, 12) else "x"
                                  for i in range(1, 14)) + "\n")
        found = [{"file": "doc.rst", "line": 3, "rule": "Gitlab", "raw": tok} for _ in range(4)]
        moved, missing = rw.relocate(str(tmp_path), found)
        lines = sorted(f["line"] for f in found)
        assert missing == 0
        assert len(set(lines)) == 4
        text = (tmp_path / "doc.rst").read_text().splitlines()
        assert all(tok in text[n - 1] for n in lines)

    def test_a_secret_not_in_the_file_is_counted_not_guessed(self, tmp_path):
        (tmp_path / "a.txt").write_text("nothing here\n")
        found = [{"file": "a.txt", "line": 1, "rule": "X", "raw": "absent-secret"}]
        assert rw.relocate(str(tmp_path), found) == (0, 1)


class TestProviderLabelMatching:
    def test_same_provider(self):
        assert tt.label_matches("TH:splunkobservabilitytoken", "trufflehog",
                                "SplunkOberservabilityToken")
        assert tt.label_matches("TH:teamworkcrm", "trufflehog", "TeamworkSpaces")
        assert tt.label_matches("AP:Google OAuth Token", "trufflehog", "GoogleOauth2")
        assert tt.label_matches("GL:gcp-api-key", "autopilot", "GCP API Key")
        assert tt.label_matches("AP:AWS Access Key ID", "gitleaks", "aws-access-token")

    def test_another_provider_or_a_format_label(self):
        assert not tt.label_matches("TH:mailboxlayer", "trufflehog", "Box")
        assert not tt.label_matches("TH:airvisual", "autopilot", "PagerDuty Integration Key")
        assert not tt.label_matches("TH:livestorm", "gitleaks", "jwt")


class TestCredentialShapedTokens:
    def test_definition(self):
        assert rw._credential_shaped("aB3dE5fG7hJ9kL1mN3pQ5rS7")
        assert not rw._credential_shaped("deadbeef" * 4)               # hex
        assert not rw._credential_shaped("abcdefghijklmnopqrstuvwx")   # no digit/upper
        assert not rw._credential_shaped("src/main/java/Foo123Bar")    # path

    def test_token_boundaries(self):
        m = rw._TOKEN.search('f(aB3dE5fG7hJ9kL1mN3pQ5rS7, x)')
        assert m and m.group(1) == "aB3dE5fG7hJ9kL1mN3pQ5rS7"
        assert re.fullmatch(r"[^=]+==", rw._TOKEN.search("k=aB3dE5fG7hJ9kL1mN3pQ5rS7==;").group(1))
