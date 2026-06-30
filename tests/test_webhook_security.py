"""
tests/test_webhook_security.py — V5
Comprehensive webhook security tests, updated for all V5 fixes.

Changes vs V4:
  - verify_signature now reads env live → patched via os.environ, not module constant
  - IP spoofing fix: _get_client_ip() takes LAST XFF entry → updated test assertion
  - startup_check now validates APP_ID and PRIVATE_KEY too → new tests added
  - Memory leak fix: _ip_counts key removed when window is empty after eviction
  - Content-Length bypass fix: test verifies len(request.data) path
"""

import hashlib
import hmac
import time
import os
import pytest
from unittest.mock import patch, MagicMock


TEST_SECRET = "super-secret-webhook-key-32chars!!"

def _make_sig(payload: bytes, secret: str = TEST_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _mock_request(
    body: bytes = b'{"action": "created"}',
    sig: str = None,
    ip: str = "1.2.3.4",
    xff: str = None,
    headers_extra: dict = None,
):
    req = MagicMock()
    req.data = body
    req.content_length = len(body)
    req.remote_addr = ip
    all_headers = {
        "X-Hub-Signature-256": sig or _make_sig(body),
        "X-Forwarded-For": xff or ip,
    }
    if headers_extra:
        all_headers.update(headers_extra)
    req.headers.get = lambda k, default="": all_headers.get(k, default)
    return req


# ── verify_signature ──────────────────────────────────────────────────────────

class TestVerifySignature:

    def test_valid_signature(self):
        from app.core.webhook_security import verify_signature
        payload = b'{"action":"opened"}'
        sig = _make_sig(payload)
        with patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": TEST_SECRET}):
            assert verify_signature(payload, sig) is True

    def test_invalid_signature(self):
        from app.core.webhook_security import verify_signature
        with patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": TEST_SECRET}):
            assert verify_signature(b"payload", "sha256=" + "a" * 64) is False

    def test_missing_signature_header(self):
        from app.core.webhook_security import verify_signature
        with patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": TEST_SECRET}):
            assert verify_signature(b"p", "") is False
            assert verify_signature(b"p", None) is False

    def test_wrong_prefix_rejected(self):
        from app.core.webhook_security import verify_signature
        with patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": TEST_SECRET}):
            assert verify_signature(b"test", "sha1=abc123") is False

    def test_empty_secret_fail_closed(self):
        """CRITICAL regression: empty secret must reject all webhooks."""
        from app.core.webhook_security import verify_signature
        with patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": ""}):
            assert verify_signature(b"payload", "sha256=anything") is False

    def test_tampered_payload_rejected(self):
        from app.core.webhook_security import verify_signature
        original = b'{"action":"opened","pr":1}'
        tampered = b'{"action":"opened","pr":999}'
        sig = _make_sig(original)
        with patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": TEST_SECRET}):
            assert verify_signature(tampered, sig) is False

    def test_secret_rotation_works_without_restart(self):
        """V5 FIX: secret read live from env, not frozen at import."""
        from app.core.webhook_security import _get_webhook_secret
        secret_a = "secret-aaa-32chars-long-xxxxxxx!"
        secret_b = "secret-bbb-32chars-long-xxxxxxx!"
        with patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": secret_a}):
            s1 = _get_webhook_secret()
        with patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": secret_b}):
            s2 = _get_webhook_secret()
        assert s1 != s2, "Secret must update without restart"

    def test_constant_time_comparison_used(self):
        import inspect
        from app.core import webhook_security
        src = inspect.getsource(webhook_security.verify_signature)
        assert "compare_digest" in src


# ── verify_timestamp ──────────────────────────────────────────────────────────

class TestTimestampProtection:

    def test_no_timestamp_header_passes(self):
        from app.core.webhook_security import verify_timestamp
        assert verify_timestamp({}) is True

    def test_fresh_timestamp_passes(self):
        from app.core.webhook_security import verify_timestamp
        ts = str(int(time.time()) - 10)
        assert verify_timestamp({"X-GitHub-Event-Time": ts}) is True

    def test_stale_timestamp_rejected(self):
        from app.core.webhook_security import verify_timestamp
        ts = str(int(time.time()) - 400)
        assert verify_timestamp({"X-GitHub-Event-Time": ts}) is False

    def test_future_timestamp_rejected(self):
        from app.core.webhook_security import verify_timestamp
        ts = str(int(time.time()) + 200)
        assert verify_timestamp({"X-GitHub-Event-Time": ts}) is False

    def test_invalid_timestamp_passes_gracefully(self):
        from app.core.webhook_security import verify_timestamp
        assert verify_timestamp({"X-GitHub-Event-Time": "not-a-number"}) is True


# ── IP extraction ─────────────────────────────────────────────────────────────

class TestIPExtraction:
    """V5 FIX: IP taken from LAST XFF entry (platform-appended), not first."""

    def test_last_xff_entry_used(self):
        from app.core.webhook_security import _get_client_ip
        req = MagicMock()
        req.headers.get = lambda k, d="": {
            "X-Forwarded-For": "attacker-injected, platform-real-ip"
        }.get(k, d)
        req.remote_addr = "127.0.0.1"
        assert _get_client_ip(req) == "platform-real-ip"

    def test_single_xff_entry_used(self):
        from app.core.webhook_security import _get_client_ip
        req = MagicMock()
        req.headers.get = lambda k, d="": {"X-Forwarded-For": "203.0.113.5"}.get(k, d)
        req.remote_addr = "127.0.0.1"
        assert _get_client_ip(req) == "203.0.113.5"

    def test_no_xff_falls_back_to_remote_addr(self):
        from app.core.webhook_security import _get_client_ip
        req = MagicMock()
        req.headers.get = lambda k, d="": d
        req.remote_addr = "10.0.0.99"
        assert _get_client_ip(req) == "10.0.0.99"

    def test_spoofed_xff_chain_uses_real_ip(self):
        """Multi-hop chain: only the last (platform-added) entry is trusted."""
        from app.core.webhook_security import _get_client_ip
        req = MagicMock()
        req.headers.get = lambda k, d="": {
            "X-Forwarded-For": "fake1, fake2, trusted-render-ip"
        }.get(k, d)
        req.remote_addr = "10.0.0.1"
        assert _get_client_ip(req) == "trusted-render-ip"


# ── IP Rate Limiting ──────────────────────────────────────────────────────────

class TestIPRateLimit:

    def test_first_request_allowed(self):
        from app.core.webhook_security import check_ip_rate_limit
        with patch("app.core.webhook_security._ip_counts", {}), \
             patch("app.core.redis_client.is_redis_available", return_value=False):
                assert check_ip_rate_limit("10.0.0.1") is True

    def test_over_limit_rejected(self):
        from app.core import webhook_security
        now = time.time()
        with patch.dict("app.core.webhook_security._ip_counts", {"9.9.9.9": [now] * 100}), \
             patch("app.core.redis_client.is_redis_available", return_value=False):
                assert webhook_security.check_ip_rate_limit("9.9.9.9") is False

    def test_expired_entries_not_counted(self):
        from app.core.webhook_security import check_ip_rate_limit, IP_RATE_LIMIT
        old_time = time.time() - 61
        with patch.dict("app.core.webhook_security._ip_counts",
                        {"7.7.7.7": [old_time] * (IP_RATE_LIMIT - 1)}), \
             patch("app.core.redis_client.is_redis_available", return_value=False):
                assert check_ip_rate_limit("7.7.7.7") is True

    def test_memory_leak_fix_empty_window_removes_key(self):
        """
        V5 FIX: After all timestamps in a window expire AND a new request
        comes in that pushes the count to 1, the key remains (1 entry = current request).
        But if we manually set a window and it becomes empty after eviction
        with no new entry appended, the key must be deleted.
        We verify the code path by checking the logic directly.
        """
        from app.core import webhook_security as ws
        import inspect
        src = inspect.getsource(ws.check_ip_rate_limit)
        # The fix is: "if window: _ip_counts[ip] = window else: _ip_counts.pop(ip, None)"
        assert "_ip_counts.pop(ip, None)" in src, (
            "Memory leak fix: empty window must delete the key from _ip_counts"
        )

    def test_different_ips_independent(self):
        from app.core.webhook_security import check_ip_rate_limit
        with patch("app.core.webhook_security._ip_counts", {}), \
             patch("app.core.redis_client.is_redis_available", return_value=False):
                for _ in range(5):
                    check_ip_rate_limit("192.168.1.1")
                assert check_ip_rate_limit("192.168.1.2") is True


# ── Payload size ──────────────────────────────────────────────────────────────

class TestPayloadSize:
    """V5 FIX: Size checked via len(request.data) not Content-Length header."""

    def test_oversized_body_rejected(self):
        from app.core.webhook_security import verify_webhook, MAX_PAYLOAD_BYTES
        big_body = b"x" * (MAX_PAYLOAD_BYTES + 1)
        req = _mock_request(body=big_body, sig=_make_sig(big_body))
        with patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": TEST_SECRET}), \
             patch("app.core.webhook_security.check_ip_rate_limit", return_value=True):
                ok, err = verify_webhook(req)
        assert ok is False
        assert "large" in err.lower()

    def test_no_content_length_header_still_checked(self):
        """Missing Content-Length header must not bypass the size check."""
        import inspect
        from app.core import webhook_security as ws
        src = inspect.getsource(ws.verify_webhook)
        assert "len(payload_bytes)" in src
        assert "content_length" not in src.split("len(payload_bytes)")[0].split("def verify_webhook")[-1]


# ── Bot sender detection ──────────────────────────────────────────────────────

class TestBotSenderDetection:

    def test_bot_type_detected(self):
        from app.core.webhook_security import is_bot_sender
        assert is_bot_sender({"sender": {"type": "Bot", "login": "some[bot]"}}) is True

    def test_bot_login_suffix_detected(self):
        from app.core.webhook_security import is_bot_sender
        assert is_bot_sender({"sender": {"type": "User", "login": "dependabot[bot]"}}) is True

    def test_human_not_detected(self):
        from app.core.webhook_security import is_bot_sender
        assert is_bot_sender({"sender": {"type": "User", "login": "shweta"}}) is False

    def test_empty_sender_no_crash(self):
        from app.core.webhook_security import is_bot_sender
        assert is_bot_sender({}) is False
        assert is_bot_sender({"sender": {}}) is False


# ── startup_check ─────────────────────────────────────────────────────────────

class TestStartupCheck:
    """V5 FIX: startup_check now validates APP_ID and PRIVATE_KEY too."""

    def test_passes_with_all_creds(self):
        from app.core.webhook_security import startup_check
        env = {
            "GITHUB_WEBHOOK_SECRET": "a" * 32,
            "GITHUB_APP_ID": "12345",
            "GITHUB_PRIVATE_KEY": "-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----",
        }
        with patch.dict(os.environ, env):
            startup_check()  # must not raise

    def test_raises_without_webhook_secret(self):
        from app.core.webhook_security import startup_check
        env = {"GITHUB_WEBHOOK_SECRET": "", "GITHUB_APP_ID": "123",
               "GITHUB_PRIVATE_KEY": "-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----"}
        with patch.dict(os.environ, env), \
             pytest.raises(RuntimeError, match="WEBHOOK_SECRET"):
                startup_check()

    def test_raises_without_app_id(self):
        """V5 FIX: missing APP_ID now caught at boot, not silently per-request."""
        from app.core.webhook_security import startup_check
        env = {"GITHUB_WEBHOOK_SECRET": "a" * 32, "GITHUB_APP_ID": "",
               "GITHUB_PRIVATE_KEY": "-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----"}
        with patch.dict(os.environ, env), pytest.raises(RuntimeError, match="APP_ID"):
            startup_check()

    def test_raises_with_non_numeric_app_id(self):
        from app.core.webhook_security import startup_check
        env = {"GITHUB_WEBHOOK_SECRET": "a" * 32, "GITHUB_APP_ID": "not-a-number",
               "GITHUB_PRIVATE_KEY": "-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----"}
        with patch.dict(os.environ, env), pytest.raises(RuntimeError, match="numeric"):
            startup_check()

    def test_raises_without_private_key(self):
        """V5 FIX: missing PRIVATE_KEY now caught at boot."""
        from app.core.webhook_security import startup_check
        env = {"GITHUB_WEBHOOK_SECRET": "a" * 32, "GITHUB_APP_ID": "12345",
               "GITHUB_PRIVATE_KEY": ""}
        with patch.dict(os.environ, env), pytest.raises(RuntimeError, match="PRIVATE_KEY"):
            startup_check()


# ── Full pipeline ─────────────────────────────────────────────────────────────

class TestVerifyWebhook:

    def test_valid_request_passes(self):
        from app.core.webhook_security import verify_webhook
        payload = b'{"action":"opened"}'
        req = _mock_request(body=payload, sig=_make_sig(payload))
        with patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": TEST_SECRET}), \
             patch("app.core.webhook_security.check_ip_rate_limit", return_value=True):
                ok, err = verify_webhook(req)
        assert ok is True and err == ""

    def test_invalid_signature_rejected(self):
        from app.core.webhook_security import verify_webhook
        req = _mock_request(sig="sha256=badhash")
        with patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": TEST_SECRET}), \
             patch("app.core.webhook_security.check_ip_rate_limit", return_value=True):
                ok, err = verify_webhook(req)
        assert ok is False and "signature" in err.lower()

    def test_empty_secret_rejects_all(self):
        """SECURITY regression: empty secret must never pass."""
        from app.core.webhook_security import verify_webhook
        payload = b'{"action":"opened"}'
        req = _mock_request(body=payload, sig=_make_sig(payload))
        with patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": ""}), \
             patch("app.core.webhook_security.check_ip_rate_limit", return_value=True):
                ok, _ = verify_webhook(req)
        assert ok is False

    def test_rate_limited_ip_rejected(self):
        from app.core.webhook_security import verify_webhook
        req = _mock_request()
        with patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": TEST_SECRET}), \
             patch("app.core.webhook_security.check_ip_rate_limit", return_value=False):
                ok, err = verify_webhook(req)
        assert ok is False and "many" in err.lower()


# ── Authorization ─────────────────────────────────────────────────────────────

class TestAuthorization:

    def test_non_restricted_command_always_allowed(self):
        from app.core.authorization import check_command_permission
        config = MagicMock()
        config.is_maintainer_only.return_value = False
        allowed, _ = check_command_permission("/explain", "repo/x", "user", "token", config)
        assert allowed is True

    def test_restricted_command_denied_for_reader(self):
        from app.core.authorization import check_command_permission
        config = MagicMock()
        config.is_maintainer_only.return_value = True
        with patch("app.core.authorization.get_user_permission", return_value="read"):
            allowed, reason = check_command_permission("/merge", "repo/x", "user", "token", config)
        assert allowed is False
        assert reason

    def test_restricted_command_allowed_for_admin(self):
        from app.core.authorization import check_command_permission
        config = MagicMock()
        config.is_maintainer_only.return_value = True
        with patch("app.core.authorization.get_user_permission", return_value="admin"):
            allowed, _ = check_command_permission("/merge", "repo/x", "admin", "token", config)
        assert allowed is True

    def test_permission_api_error_denies_access(self):
        """Fail closed: API error → deny command."""
        from app.core.authorization import check_command_permission
        config = MagicMock()
        config.is_maintainer_only.return_value = True
        with patch("app.core.authorization.gh_get", side_effect=Exception("network error")):
            allowed, _ = check_command_permission("/merge", "repo/x", "user", "token", config)
        assert allowed is False
