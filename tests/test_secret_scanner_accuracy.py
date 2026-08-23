"""
tests/test_secret_scanner_accuracy.py

Accuracy corpus for the secret scanner: a fixed set of strings that must NEVER
be reported, and a fixed set that must ALWAYS be reported.

Both directions matter and both were broken:

  False positives — the entropy heuristic fired on lockfile integrity hashes and
  other high-entropy-by-design values. A scanner that cries wolf is one whose
  real findings stop being read.

  False negatives — the gate required >= 4.5 bits of Shannon entropy per
  character, but entropy is bounded by log2(min(alphabet, length)). Hex tops out
  at 4.0, so a real hex API key could never clear the bar; a 24-character secret
  tops out at 4.58 and cleared it only by luck. Those patterns were dead by
  arithmetic, not by choice.

Secrets here are generated randomly at runtime rather than hardcoded, so this
file never contains a credential-shaped literal that GitHub's own secret
scanning (or this scanner, run on this repo) would flag.
"""

import logging
import secrets as _secrets
import string

import pytest

from app.security.enhanced_secrets import (
    MIN_DISTINCT_CHARS,
    _PLACEHOLDER_WORDS,
    _entropy,
    _entropy_ratio,
    _is_structural_non_secret,
    _looks_random,
    scan_diff,
)

HEX = "0123456789abcdef"
LOWER_ALNUM = string.ascii_lowercase + string.digits
ALNUM = string.ascii_letters + string.digits

SCANNED_FILE = "app/settings.py"  # not a test/docs path, so nothing is skipped


def _rand(alphabet: str, n: int) -> str:
    return "".join(_secrets.choice(alphabet) for _ in range(n))


@pytest.fixture(autouse=True)
def _quiet():
    """scan_diff logs a warning per detection; silence it for readable output."""
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


# ── Must never be reported ───────────────────────────────────────────────────

BENIGN = {
    "git commit sha": '+COMMIT = "4657a631bc512ea27be1e0c7155f28c2907c99bc"',
    "uuid4": '+SESSION = "550e8400-e29b-41d4-a716-446655440000"',
    "sha256 digest": (
        '+DIGEST = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"'
    ),
    "md5 digest": '+ETAG = "5d41402abc4b2a76b9719d911017c592"',
    "npm lockfile integrity": (
        '+  "integrity": "sha512-nQyp7sfE7RUqhKQ8bB9pdcM8UGmPJmVEuP8bBqBcCwLP'
        'YqAvzKvKuTAwSbA5A6zSGSJHTIpBc0vTLXrTvYyMLQ"'
    ),
    "subresource integrity": (
        '+<script integrity="sha384-oqVuAfXRKap7fdgcCY5uykM6R9GqQ8Kuxy9rx7HNQ'
        'lGYl1kPzQho1wx4JwY8wC">'
    ),
    "oci image digest": (
        '+image: "app@sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934c'
        'a495991b7852b855"'
    ),
    "png data uri": '+ICON = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"',
    "base64 png blob": (
        '+LOGO = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk"'
    ),
    "generated css classes": '+CLASS = "sc-bdVaJa kKzKdX jsx-2891594908 css-1x2y3z4a"',
    "placeholder key": '+API_KEY = "your-api-key-here-replace-me-please"',
    "repeated pattern": '+X = "abcabcabcabcabcabcabcabcabcabcabc"',
    "base64 certificate chunk": '+CERT = "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA"',
}


@pytest.mark.parametrize("name", sorted(BENIGN))
def test_benign_value_is_never_reported(name):
    findings = scan_diff(BENIGN[name], file_path=SCANNED_FILE)
    assert findings == [], (
        f"false positive on {name}: "
        f"{[f.pattern_name for f in findings]}. A scanner that flags ordinary "
        f"diff content is one whose real findings get ignored."
    )


@pytest.mark.parametrize("length", [32, 40, 64, 128])
def test_random_hex_digests_are_never_reported(length):
    """Every standard digest length, with maximally random content."""
    findings = scan_diff(f'+H = "{_rand(HEX, length)}"', file_path=SCANNED_FILE)
    assert findings == [], f"{length}-char hex digest reported as a secret"


# ── Must always be reported ──────────────────────────────────────────────────


def _real_secret_cases():
    """Built fresh each call so no credential-shaped literal is committed."""
    return {
        "aws secret access key": f'+aws_secret_access_key = "{_rand(ALNUM + "/+", 40)}"',
        "aws session token": f'+aws_session_token = "{_rand(ALNUM + "/+=", 120)}"',
        "aws access key id": '+key = "AKIA3XYZQ7MNPL2KDR8T"',
        "firebase api key": f'+firebase_key = "{_rand(ALNUM + "_-", 37)}"',
        "azure client secret": f'+azure_client_secret = "{_rand(ALNUM + "~._-", 34)}"',
        "twilio auth token": f'+twilio_auth_token = "{_rand(LOWER_ALNUM, 32)}"',
        "cloudflare api key (hex)": f'+cloudflare_key = "{_rand(HEX, 37)}"',
        "cloudflare api token": f'+cloudflare_token = "{_rand(ALNUM + "_-", 40)}"',
        "dockerhub pat": f'+dockerhub_pat = "{_rand(ALNUM + "_-", 36)}"',
        "datadog api key (hex)": f'+datadog_api_key = "{_rand(HEX, 32)}"',
        "generic password": f'+password = "{_rand(ALNUM + "!@#$", 24)}"',
        "generic api key": f'+api_key = "{_rand(ALNUM, 40)}"',
        "github pat": f'+t = "ghp_{_rand(ALNUM, 36)}"',
        "groq api key": f'+g = "gsk_{_rand(ALNUM, 52)}"',
        "slack bot token (10-digit team)": (
            f'+S = "xoxb-2914837465-2918374651234-{_rand(ALNUM, 24)}"'
        ),
        "slack bot token (11-digit team)": (
            f'+S = "xoxb-29148374651-29183746512-{_rand(ALNUM, 24)}"'
        ),
        # The header alone is a format string, not a key — it appears verbatim
        # in this scanner's own ruleset. What is asserted here is a key with
        # actual material, which is what a leak looks like.
        "rsa private key with material": (
            "+-----BEGIN RSA PRIVATE KEY-----\n"
            "+MIIEpAIBAAKCAQEA7Xk9pQm2vRtYhL3nWcF4dJ8sKzB1gTaV6uNxE0oPqHrCmZyD\n"
            "+-----END RSA PRIVATE KEY-----"
        ),
        "postgres connection string": '+DB = "postgresql://admin:hunter2swordfish@db:5432/prod"',
    }


@pytest.mark.parametrize("name", sorted(_real_secret_cases()))
def test_real_secret_is_always_reported(name):
    """
    Repeated across independent random samples on purpose.

    A single draw only shows that detection is *possible*. Short secrets
    under-sample their own alphabet by chance, so a threshold tuned against one
    lucky sample makes detection probabilistic — a scanner that finds a
    credential nine times out of ten is not a scanner anyone can rely on.
    """
    misses = []
    for _ in range(40):
        diff = _real_secret_cases()[name]
        if not scan_diff(diff, file_path=SCANNED_FILE):
            misses.append(diff)
    assert not misses, (
        f"MISSED a real {name} on {len(misses)}/40 random samples — detection "
        f"is probabilistic. Entropy is capped at log2(min(alphabet, length)) "
        f"bits per character, and short strings fall well below that ceiling "
        f"by chance."
    )


# Every placeholder word, embedded as a WHOLE TOKEN in an otherwise real
# credential. This is the shape that a randomised test only finds by luck:
# the generated Docker Hub PAT that exposed it drew "xxx" between two
# separators at a rate of roughly 1 in 30,000, so the 40-sample loop above
# caught it on one CI run and would have passed on the next twenty.
#
# Built by construction instead. Each case is a real, high-entropy secret
# that happens to contain one placeholder word, and every one of them must
# be reported.
def _secret_carrying_placeholder_word(word: str) -> str:
    head = _rand(ALNUM, 14)
    tail = _rand(ALNUM, 12)
    return f'+dockerhub_pat = "{head}_{word}-{tail}"'


@pytest.mark.parametrize("word", sorted(_PLACEHOLDER_WORDS))
def test_placeholder_word_inside_real_key_material_is_still_reported(word):
    """
    A placeholder word only means something when nothing ELSE in the value
    looks like a secret.

    Whole-word matching fixed words buried inside a token, but a generated
    credential contains separators of its own, and the fragments between them
    are whole words in exactly that sense. A human writing a stand-in writes
    only stand-in text — never a 14-character random string with one
    placeholder-shaped fragment in it.
    """
    diff = _secret_carrying_placeholder_word(word)
    assert scan_diff(diff, file_path=SCANNED_FILE), (
        f"MISSED a real credential because it contained the placeholder word "
        f"{word!r} as a token: {diff}. A false negative in a secret scanner is "
        f"the one failure that cannot be noticed in production."
    )


def test_a_value_that_is_only_placeholder_words_is_still_suppressed():
    """The guard above must not reopen the false-positive direction."""
    for value in (
        "your_api_key_here",
        "REPLACE_WITH_RANDOM",
        "changeme",
        "insert-your-token-here",
        "my_secret_placeholder",
    ):
        assert not scan_diff(f'+api_key = "{value}"', file_path=SCANNED_FILE), (
            f"{value!r} is a placeholder and must never be reported"
        )


@pytest.mark.parametrize("length", [10, 11, 13])
def test_slack_team_ids_of_any_width_are_detected(length):
    """The hardcoded {11} missed real tokens with 10- or 13-digit team IDs."""
    team = _rand(string.digits, length)
    bot = _rand(string.digits, length)
    diff = f'+S = "xoxb-{team}-{bot}-{_rand(ALNUM, 24)}"'
    assert scan_diff(diff, file_path=SCANNED_FILE), f"{length}-digit Slack team ID missed"


# ── The entropy measure itself ───────────────────────────────────────────────


class TestEntropyCeiling:
    def test_hex_cannot_reach_the_old_flat_threshold(self):
        """The arithmetic that silently disabled the hex patterns."""
        best = max(_entropy(_rand(HEX, 37)) for _ in range(500))
        assert best < 4.5, "hex should be bounded below the old 4.5-bit gate"

    @pytest.mark.parametrize("length", [32, 37, 40, 64])
    def test_hex_is_reliably_recognised_as_random(self, length):
        """...and the ratio measure sees it correctly, every time. A 32-char
        hex string can land on as few as 9 distinct characters by chance, so
        the floor has to sit below that."""
        failures = [
            s for s in (_rand(HEX, length) for _ in range(200)) if not _looks_random(s)
        ]
        assert not failures, (
            f"{len(failures)}/200 random {length}-char hex values were not "
            f"recognised as random; worst sample had "
            f"{min(len(set(f)) for f in failures)} distinct characters"
        )

    @pytest.mark.parametrize(
        "alphabet,length", [(HEX, 40), (LOWER_ALNUM, 32), (ALNUM, 48)]
    )
    def test_ratio_is_scale_free_across_alphabets(self, alphabet, length):
        """The point of the ratio: hex and base64 secrets score alike, where
        raw bits-per-character put them two full bits apart."""
        worst = min(_entropy_ratio(_rand(alphabet, length)) for _ in range(200))
        assert worst > 0.80

    def test_repetitive_string_is_rejected_despite_perfect_ratio(self):
        """"abcabc..." uses its alphabet perfectly uniformly — ratio alone would
        accept it, which is why the distinct-character floor exists."""
        s = "abc" * 12
        assert _entropy_ratio(s) > 0.99
        assert _looks_random(s) is False

    def test_single_character_string_is_not_random(self):
        assert _looks_random("a" * 40) is False

    def test_short_string_is_not_random(self):
        assert _looks_random("ab") is False

    @pytest.mark.parametrize("bad", ["", "a", "aa"])
    def test_degenerate_input_has_zero_ratio(self, bad):
        assert _entropy_ratio(bad) == 0.0

    def test_distinct_floor_is_what_excludes_hex_from_the_unanchored_path(self):
        """Hex has 16 symbols; the unanchored detector requires 20, so no
        digest can ever reach it however random it looks."""
        assert len(set(HEX)) < 20
        assert MIN_DISTINCT_CHARS <= 9, (
            "anchored gate must accept hex, which can land on 9 distinct "
            "characters in a 32-char sample"
        )


class TestStructuralRecognition:
    @pytest.mark.parametrize(
        "value",
        [
            "5d41402abc4b2a76b9719d911017c592",  # md5
            "4657a631bc512ea27be1e0c7155f28c2907c99bc",  # sha1 / git
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "sha512-nQyp7sfE7RUqhKQ8bB9pdcM8UGmPJmVEuP8bBqBc",
            "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4",
            "550e8400-e29b-41d4-a716-446655440000",
            "data:image/png;base64,iVBORw0KGgo",
            "iVBORw0KGgoAAAANSUhEUgAAAAE",
        ],
    )
    def test_recognised_as_non_secret(self, value):
        assert _is_structural_non_secret(value) is True

    @pytest.mark.parametrize(
        "value",
        ["ghp_" + "a" * 36, "AKIA3XYZQ7MNPL2KDR8T", "not-a-hash-at-all"],
    )
    def test_credentials_are_not_mistaken_for_digests(self, value):
        assert _is_structural_non_secret(value) is False

    def test_hex_of_nonstandard_length_is_not_auto_excluded(self):
        """37 hex chars is not a digest length — a Cloudflare key is exactly
        that shape, so the structural filter must not swallow it."""
        assert _is_structural_non_secret("a1b2c3d4e5f60718293a4b5c6d7e8f9012345") is False
