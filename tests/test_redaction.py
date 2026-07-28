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
