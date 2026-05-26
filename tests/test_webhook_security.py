"""
tests/test_webhook_security.py
────────────────────────────────
Comprehensive tests for webhook verification pipeline.

Tests cover:
  - Signature verification (valid, invalid, missing, empty secret)
  - Replay protection (old timestamps, future timestamps)
  - IP rate limiting
  - Bot loop prevention
  - Full verify_webhook() pipeline
  - Startup check behavior
  - Edge cases (malformed headers, oversized payloads)

Run: pytest tests/test_webhook_security.py -v
"""

import hashlib
import hmac
import time
import pytest
from unittest.mock import patch, MagicMock


# ── Helpers ───────────────────────────────────────────────────────────────────

TEST_SECRET = b"super-secret-webhook-key-32chars!!"

def _make_sig(payload: bytes, secret: bytes = TEST_SECRET) -> str:
    return "sha256=" + hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _mock_request(
    body: bytes = b'{"action": "created"}',
    sig: str = None,
    content_length: int = None,
    ip: str = "1.2.3.4",
    headers_extra: dict = None,
):
    """Build a mock Flask request object."""
    req = MagicMock()
    req.data = body
    req.content_length = content_length or len(body)
    req.remote_addr = ip
    req.headers = MagicMock()

    all_headers = {
        "X-Hub-Signature-256": sig or _make_sig(body),
        "X-Forwarded-For": ip,
    }
    if headers_extra:
        all_headers.update(headers_extra)

    req.headers.get = lambda k, default="": all_headers.get(k, default)
    return req


# ── verify_signature tests ────────────────────────────────────────────────────

class TestVerifySignature:

    def test_valid_signature(self):
        from app.core.webhook_security import verify_signature
        payload = b'{"action":"opened"}'
        sig = _make_sig(payload)
        with patch("app.core.webhook_security.WEBHOOK_SECRET", TEST_SECRET):
            assert verify_signature(payload, sig) is True

    def test_invalid_signature(self):
        from app.core.webhook_security import verify_signature
        payload = b'{"action":"opened"}'
        bad_sig = "sha256=" + "a" * 64
        with patch("app.core.webhook_security.WEBHOOK_SECRET", TEST_SECRET):
            assert verify_signature(payload, bad_sig) is False

    def test_missing_signature_header(self):
        from app.core.webhook_security import verify_signature
        with patch("app.core.webhook_security.WEBHOOK_SECRET", TEST_SECRET):
            assert verify_signature(b"payload", "") is False
            assert verify_signature(b"payload", None) is False

    def test_wrong_prefix(self):
        from app.core.webhook_security import verify_signature
        payload = b"test"
        with patch("app.core.webhook_security.WEBHOOK_SECRET", TEST_SECRET):
            # sha1= prefix should fail
            assert verify_signature(payload, "sha1=abc123") is False

    def test_empty_secret_returns_false(self):
        """CRITICAL: Empty secret must FAIL CLOSED, not bypass verification."""
        from app.core.webhook_security import verify_signature
        with patch("app.core.webhook_security.WEBHOOK_SECRET", b""):
            result = verify_signature(b"any payload", "sha256=anything")
            assert result is False, (
                "Empty WEBHOOK_SECRET must reject all webhooks (fail closed). "
                "This was the original security bug — empty secret was bypassing verification!"
            )

    def test_tampered_payload_rejected(self):
        """Signature on original payload must not verify tampered payload."""
        from app.core.webhook_security import verify_signature
        original = b'{"action":"opened","pr":1}'
        tampered = b'{"action":"opened","pr":999}'
        sig = _make_sig(original)
        with patch("app.core.webhook_security.WEBHOOK_SECRET", TEST_SECRET):
            assert verify_signature(tampered, sig) is False

    def test_constant_time_comparison(self):
        """Ensure hmac.compare_digest is used (not == which is timing-vulnerable)."""
        import inspect
        from app.core import webhook_security
        source = inspect.getsource(webhook_security.verify_signature)
        assert "compare_digest" in source, (
            "verify_signature must use hmac.compare_digest for constant-time comparison"
        )


# ── Timestamp / replay tests ──────────────────────────────────────────────────

class TestTimestampProtection:

    def test_no_timestamp_header_passes(self):
        """If GitHub doesn't send timestamp, we skip the check."""
        from app.core.webhook_security import verify_timestamp
        assert verify_timestamp({}) is True

    def test_fresh_timestamp_passes(self):
        from app.core.webhook_security import verify_timestamp
        ts = str(int(time.time()) - 10)   # 10 seconds ago
        assert verify_timestamp({"X-GitHub-Event-Time": ts}) is True

    def test_stale_timestamp_rejected(self):
        from app.core.webhook_security import verify_timestamp
        ts = str(int(time.time()) - 400)   # 400 seconds ago (> MAX_AGE_SECONDS=300)
        assert verify_timestamp({"X-GitHub-Event-Time": ts}) is False

    def test_future_timestamp_rejected(self):
        from app.core.webhook_security import verify_timestamp
        ts = str(int(time.time()) + 200)   # 200 seconds in the future
        assert verify_timestamp({"X-GitHub-Event-Time": ts}) is False

    def test_invalid_timestamp_header_passes(self):
        """Malformed timestamp should not crash — just skip the check."""
        from app.core.webhook_security import verify_timestamp
        assert verify_timestamp({"X-GitHub-Event-Time": "not-a-number"}) is True


# ── IP Rate Limiting tests ─────────────────────────────────────────────────────

class TestIPRateLimit:

    def test_first_request_allowed(self):
        from app.core.webhook_security import check_ip_rate_limit
        with patch("app.core.webhook_security._ip_counts", {}):
            assert check_ip_rate_limit("10.0.0.1") is True

    def test_at_limit_allowed(self):
        from app.core import webhook_security
        from unittest.mock import patch
        now = time.time()
        with patch.dict("app.core.webhook_security._ip_counts",
                        {"9.9.9.9": [now] * 100}):
            with patch("app.core.redis_client.is_redis_available", return_value=False):
                assert webhook_security.check_ip_rate_limit("9.9.9.9") is False

    def test_different_ips_independent(self):
        from app.core.webhook_security import check_ip_rate_limit
        with patch("app.core.webhook_security._ip_counts", {}):
            with patch("app.core.redis_client.is_redis_available", return_value=False):
                for _ in range(5):
                    check_ip_rate_limit("192.168.1.1")
                # Different IP should still pass
                assert check_ip_rate_limit("192.168.1.2") is True

    def test_old_requests_not_counted(self):
        """Requests older than 60s should be evicted from window."""
        from app.core.webhook_security import check_ip_rate_limit
        old_time = time.time() - 61   # 61 seconds ago = outside window
        with patch("app.core.webhook_security._ip_counts", {"7.7.7.7": [old_time] * 99}):
            with patch("app.core.redis_client.is_redis_available", return_value=False):
                assert check_ip_rate_limit("7.7.7.7") is True


# ── Bot sender detection ──────────────────────────────────────────────────────

class TestBotSenderDetection:

    def test_bot_type_detected(self):
        from app.core.webhook_security import is_bot_sender
        payload = {"sender": {"type": "Bot", "login": "some-app[bot]"}}
        assert is_bot_sender(payload) is True

    def test_bot_login_suffix_detected(self):
        from app.core.webhook_security import is_bot_sender
        payload = {"sender": {"type": "User", "login": "dependabot[bot]"}}
        assert is_bot_sender(payload) is True

    def test_human_sender_not_detected(self):
        from app.core.webhook_security import is_bot_sender
        payload = {"sender": {"type": "User", "login": "shweta"}}
        assert is_bot_sender(payload) is False

    def test_own_bot_detected(self):
        from app.core.webhook_security import is_bot_sender
        payload = {"sender": {"type": "Bot", "login": "ai-repo-manager[bot]"}}
        assert is_bot_sender(payload) is True

    def test_empty_sender(self):
        """Must not crash on missing/partial payload."""
        from app.core.webhook_security import is_bot_sender
        assert is_bot_sender({}) is False
        assert is_bot_sender({"sender": {}}) is False


# ── Full pipeline tests ────────────────────────────────────────────────────────

class TestVerifyWebhook:

    def test_valid_request_passes(self):
        from app.core.webhook_security import verify_webhook
        payload = b'{"action":"opened"}'
        req = _mock_request(body=payload, sig=_make_sig(payload))
        with patch("app.core.webhook_security.WEBHOOK_SECRET", TEST_SECRET):
            ok, err = verify_webhook(req)
        assert ok is True
        assert err == ""

    def test_invalid_signature_rejected(self):
        from app.core.webhook_security import verify_webhook
        payload = b'{"action":"opened"}'
        req = _mock_request(body=payload, sig="sha256=badhash")
        with patch("app.core.webhook_security.WEBHOOK_SECRET", TEST_SECRET):
            ok, err = verify_webhook(req)
        assert ok is False
        assert "signature" in err.lower()

    def test_oversized_payload_rejected(self):
        from app.core.webhook_security import verify_webhook, MAX_PAYLOAD_BYTES
        req = _mock_request(content_length=MAX_PAYLOAD_BYTES + 1)
        with patch("app.core.webhook_security.WEBHOOK_SECRET", TEST_SECRET):
            ok, err = verify_webhook(req)
        assert ok is False
        assert "large" in err.lower()

    def test_missing_secret_rejects_all(self):
        """
        SECURITY: Empty secret must reject all webhooks.
        Previously this was a bypass. Regression test to prevent re-introduction.
        """
        from app.core.webhook_security import verify_webhook
        payload = b'{"action":"opened"}'
        req = _mock_request(body=payload, sig=_make_sig(payload))
        with patch("app.core.webhook_security.WEBHOOK_SECRET", b""):
            ok, err = verify_webhook(req)
        assert ok is False, (
            "REGRESSION: Empty WEBHOOK_SECRET must never allow webhooks through. "
            "This was a critical security bug — do not revert this behavior."
        )

    def test_x_forwarded_for_extracted_for_rate_limit(self):
        """Rate limit uses first IP from X-Forwarded-For, not remote_addr."""
        from app.core.webhook_security import verify_webhook
        payload = b'{"action":"opened"}'
        req = _mock_request(
            body=payload,
            sig=_make_sig(payload),
            headers_extra={"X-Forwarded-For": "203.0.113.1, 10.0.0.1"},
        )
        # Just check it doesn't crash and processes the right IP
        with patch("app.core.webhook_security.WEBHOOK_SECRET", TEST_SECRET):
            with patch("app.core.webhook_security.check_ip_rate_limit", return_value=True) as mock_rl:
                verify_webhook(req)
                called_ip = mock_rl.call_args[0][0]
                assert called_ip == "203.0.113.1"


# ── Startup check tests ───────────────────────────────────────────────────────

class TestStartupCheck:

    def test_startup_check_passes_with_secret(self):
        from app.core.webhook_security import startup_check
        with patch.dict("os.environ", {"GITHUB_WEBHOOK_SECRET": "a" * 32}):
            startup_check()   # Should not raise

    def test_startup_check_raises_without_secret(self):
        from app.core.webhook_security import startup_check
        with patch.dict("os.environ", {"GITHUB_WEBHOOK_SECRET": ""}):
            with pytest.raises(RuntimeError, match="GITHUB_WEBHOOK_SECRET"):
                startup_check()

    def test_startup_check_warns_about_weak_secret(self, caplog):
        from app.core.webhook_security import startup_check
        import logging
        with patch.dict("os.environ", {"GITHUB_WEBHOOK_SECRET": "short"}):
            with caplog.at_level(logging.WARNING):
                startup_check()
            assert "weak_secret" in caplog.text or "short" in caplog.text


# ── Authorization tests ───────────────────────────────────────────────────────

class TestAuthorization:

    def test_non_restricted_command_always_allowed(self):
        from app.core.authorization import check_command_permission
        config = MagicMock()
        config.is_maintainer_only.return_value = False
        allowed, reason = check_command_permission("/explain", "repo/x", "user", "token", config)
        assert allowed is True

    def test_restricted_command_denied_for_non_maintainer(self):
        from app.core.authorization import check_command_permission
        config = MagicMock()
        config.is_maintainer_only.return_value = True
        with patch("app.core.authorization.get_user_permission", return_value="read"):
            allowed, reason = check_command_permission("/merge", "repo/x", "user", "token", config)
        assert allowed is False
        assert "permission" in reason.lower() or "access" in reason.lower()

    def test_restricted_command_allowed_for_maintainer(self):
        from app.core.authorization import check_command_permission
        config = MagicMock()
        config.is_maintainer_only.return_value = True
        with patch("app.core.authorization.get_user_permission", return_value="admin"):
            allowed, reason = check_command_permission("/merge", "repo/x", "admin_user", "token", config)
        assert allowed is True

    def test_permission_api_error_denies_access(self):
        """Fail closed: if permission API errors, deny the command."""
        from app.core.authorization import check_command_permission
        config = MagicMock()
        config.is_maintainer_only.return_value = True
        with patch("app.core.authorization.gh_get", side_effect=Exception("network error")):
            allowed, _ = check_command_permission("/merge", "repo/x", "user", "token", config)
        assert allowed is False

    def test_permission_cache_used_on_second_call(self):
        """Permission API should not be called twice for same user within TTL."""
        from app.core.authorization import get_user_permission, _perm_cache
        _perm_cache.clear()
        with patch("app.core.authorization.gh_get", return_value={"permission": "write"}) as mock_gh:
            p1 = get_user_permission("repo/x", "user", "token")
            p2 = get_user_permission("repo/x", "user", "token")
        assert p1 == p2 == "write"
        mock_gh.assert_called_once()   # Cache hit on second call

    def test_404_returns_none_permission(self):
        """User not in collaborators → permission = none."""
        from app.core.authorization import get_user_permission
        from app.github.client import GitHubError
        with patch("app.core.authorization.gh_get", side_effect=GitHubError("not found", 404)):
            perm = get_user_permission("repo/x", "outsider", "token")
        assert perm == "none"
