"""
Secret-scanner defects found by the 2026-10-08 audit, each pinned here.

Credentials are assembled at runtime so this file does not trip GitHub's own
secret scanning.
"""

import random
import string

from app.security.enhanced_secrets import _redact, scan_diff


def _diff(line: str, start: int = 1) -> str:
    return f"@@ -0,0 +{start},1 @@\n+{line}"


def _pat() -> str:
    return "ghp_" + "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3" + "zA5"


def _akia() -> str:
    return "AKIA" + "Q3EGRJ7XPLMN4WZT"


def _rand(n: int) -> str:
    rng = random.Random(n)
    return "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(n))


def _names(findings) -> list[str]:
    return [f.pattern_name for f in findings]


class TestACommentNeverHidesAVendorToken:
    """`# test`, `# example`, `mock`, `fake` skipped the whole line before any
    pattern ran, including `ghp_…` and `AKIA…`."""

    def test_a_test_comment_after_a_github_token(self):
        found = scan_diff(_diff(f'GITHUB_TOKEN = "{_pat()}"  # test token for CI'))
        assert "GitHub PAT (classic)" in _names(found)

    def test_an_example_comment_after_an_aws_key(self):
        found = scan_diff(_diff(f'AWS_ACCESS_KEY_ID = "{_akia()}"  # example from prod config'))
        assert "AWS Access Key ID" in _names(found)

    def test_the_word_mock_on_the_line(self):
        found = scan_diff(_diff(f'TOKEN = "{_pat()}"  # replaces the mock'))
        assert "GitHub PAT (classic)" in _names(found)

    def test_a_weak_keyword_match_on_a_test_line_is_still_discounted(self):
        found = scan_diff(_diff(f'password = "{_rand(24)}"  # example'))
        assert found == []


class TestCommonFormatsAreFound:
    def test_postgres_scheme(self):
        found = scan_diff(_diff(f"DATABASE_URL=postgres://app:{_rand(20)}@db.example.net:5432/app"))
        assert "Connection String" in _names(found)

    def test_mongodb_srv(self):
        found = scan_diff(_diff(f'uri = "mongodb+srv://svc:{_rand(18)}@cluster0.x.mongodb.net/db"'))
        assert "Connection String" in _names(found)

    def test_rediss(self):
        found = scan_diff(_diff(f"REDIS_URL=rediss://default:{_rand(22)}@r.example.net:6379"))
        assert "Connection String" in _names(found)

    def test_placeholder_and_default_passwords_are_not_leaks(self):
        for url in (
            "postgres://user:<password>@localhost/db",
            "postgres://postgres:postgres@db:5432/app",
            "mysql://root:${DB_PASS}@db/app",
            "rediss://default:…@host",
        ):
            assert scan_diff(_diff(f"url = '{url}'")) == [], url

    def test_slack_webhook_with_longer_ids(self):
        url = "https://hooks.slack.com/services/T0ABCDEFGH1/B0ABCDEFGH2/" + _rand(24)
        assert "Slack Webhook" in _names(scan_diff(_diff(f'HOOK = "{url}"')))

    def test_aws_secret_key_unquoted_as_in_a_credentials_file(self):
        secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCY" + "zQ8Rk3Lm2N"
        assert "AWS Secret Access Key" in _names(scan_diff(_diff(f"aws_secret_access_key = {secret}")))
        assert "AWS Secret Access Key" in _names(scan_diff(_diff(f"AWS_SECRET_ACCESS_KEY={secret}")))

    def test_an_unquoted_env_file_secret(self):
        found = scan_diff(_diff(f"STRIPE_WEBHOOK_SECRET={_rand(32)}"), file_path=".env")
        assert "Env File Secret" in _names(found)

    def test_code_assigning_a_function_result_is_not_an_env_secret(self):
        assert scan_diff(_diff("API_TOKEN = generate_session_token_for_user")) == []


class TestNamesAreNotCredentials:
    """Each of these opened a 'Rotate ALL exposed credentials NOW' issue."""

    def test_env_var_names_class_paths_and_header_names(self):
        for line in (
            'TOKEN_ENV_VAR = "GITHUB_PERSONAL_ACCESS_TOKEN"',
            'SECRET_NAME = "PRODUCTION_DATABASE_PASSWORD"',
            'token_class = "rest_framework.authtoken.TokenAuthentication"',
            'api_key_header = "X-Goog-Api-Key-Override-Header"',
        ):
            assert scan_diff(_diff(line)) == [], line

    def test_a_real_generic_token_is_still_found(self):
        found = scan_diff(_diff(f'auth_token = "{_rand(32)}"'))
        assert "Generic Token" in _names(found)


class TestLineNumbersAreFileLines:
    def test_a_token_on_file_line_102(self):
        patch = "@@ -100,3 +100,4 @@\n a = 1\n b = 2\n+TOKEN = '" + _pat() + "'\n c = 3"
        (finding,) = [f for f in scan_diff(patch) if f.pattern_name == 'GitHub PAT (classic)']
        assert finding.line_number == 102

    def test_removed_lines_do_not_advance_the_count(self):
        patch = "@@ -10,3 +10,3 @@\n a = 1\n-old = 2\n+TOKEN = '" + _pat() + "'"
        (finding,) = [f for f in scan_diff(patch) if f.pattern_name == 'GitHub PAT (classic)']
        assert finding.line_number == 11


class TestRedactionKeepsNothingOfAKeywordValue:
    def test_password_value(self):
        out = _redact('password = "Zq8!vT2#pLm9"')
        assert "Lm9" not in out and "Zq8" not in out and out.startswith("password")

    def test_connection_string_password(self):
        out = _redact("mysql://root:hunter22@")
        assert "r22" not in out and "hunt" not in out and out == "mysql://root:****@"

    def test_unquoted_assignment(self):
        out = _redact("aws_secret_access_key = " + "wJalrXUtnFEMI/K7MDENG/bPxRfiCYzQ8Rk3Lm2N")
        assert "Lm2N" not in out and "wJal" not in out

    def test_bare_token_keeps_its_prefix_and_last_four(self):
        token = _pat()
        out = _redact(token)
        assert out.startswith("ghp_") and out.endswith(token[-4:]) and token[8:20] not in out
