"""tests/test_redaction.py — scrub text before it enters long-lived memory."""

from app.core.redaction import redact


class TestRedaction:
    def test_aws_key_is_removed(self):
        out = redact("the key is AKIAIOSFODNN7REALKEY here")
        assert "AKIAIOSFODNN7REALKEY" not in out
        assert "[REDACTED]" in out

    def test_github_pat_is_removed(self):
        pat = "ghp_" + "a" * 36
        out = redact(f"token {pat} committed by mistake")
        assert pat not in out

    def test_fenced_code_is_stripped(self):
        out = redact("before\n```python\nsecret_business_logic()\n```\nafter")
        assert "secret_business_logic" not in out
        assert "before" in out
        assert "after" in out

    def test_indented_code_block_is_stripped(self):
        out = redact("summary line\n    proprietary_algorithm(x, y)\nend")
        assert "proprietary_algorithm" not in out
        assert "summary line" in out

    def test_ordinary_prose_survives(self):
        text = "The auth handler rejects expired sessions before touching the DB."
        assert redact(text) == text

    def test_file_paths_and_symbols_survive(self):
        """Memory is useful precisely because it keeps these."""
        text = "Fix accepted in app/handlers/push.py — _already_reported now fails closed."
        out = redact(text)
        assert "app/handlers/push.py" in out
        assert "_already_reported" in out

    def test_empty_input(self):
        assert redact("") == ""

    def test_none_safe(self):
        assert redact(None) == ""


class TestAudit2:
    """Found by the 2026-10-08 audit."""

    def test_a_keyword_value_is_not_left_behind(self):
        from app.core.redaction import redact

        value = "Xk9#pQ2vL7mN4wR8tZ"
        out = redact(f'set db_password = "{value}" before deploying')
        assert value not in out and "before deploying" in out

    def test_a_short_value_is_masked_too(self):
        """Matches of 12 characters or fewer were skipped entirely."""
        from app.core.redaction import redact

        out = redact("db: mysql://root:hunter2x9@db.internal/app")
        assert "hunter2x9" not in out

    def test_a_line_without_a_secret_is_untouched(self):
        from app.core.redaction import redact

        text = "Use the token_count field when budgeting = yes"
        assert redact(text) == text

    def test_webhook_path_in_a_urllib3_error_is_redacted(self):
        from app.core.redaction import redact_secrets

        err = (
            "HTTPSConnectionPool(host='hooks.slack.com', port=443): Max retries exceeded "
            "with url: /services/T0000AAAA/B0000BBBB/SlAcKsEcReTvAlUe (Caused by ...)"
        )
        out = redact_secrets(err)
        assert "SlAcKsEcReTvAlUe" not in out and "/services/REDACTED" in out

    def test_discord_webhook_path_is_redacted(self):
        from app.core.redaction import redact_secrets

        out = redact_secrets("with url: /api/webhooks/123456789/AbCdEfToKeN (Caused by")
        assert "AbCdEfToKeN" not in out
