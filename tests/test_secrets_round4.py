"""
tests/test_secrets_round4.py

Round-4 secrets audit. Each case was reproduced against the code before the
fix: a webhook secret, a Redis or Ollama password, or a key prefix reached a
log line, /health or a public GitHub comment.
"""

from __future__ import annotations

import base64
import logging
import secrets
from unittest.mock import patch

import pytest

from app.core.redaction import redact_secrets

TOKEN = "ZZSECRETzz9876543210abcd"


@pytest.mark.parametrize(
    "text",
    [
        f"https://hooks.slack.com/triggers/T0ABC/123456/{TOKEN}",
        f"https://hooks.slack.com/workflows/T0ABC/A0123/456/{TOKEN}",
        f"https://hooks.slack.com/services/t0abc/b0def/{TOKEN}",
        f"Invalid URL 'hooks.slack.com/services/T0ABC/B0DEF/{TOKEN}': No scheme supplied",
        f"No connection adapters were found for 'htps://hooks.slack.com/services/T0/B0/{TOKEN}'",
        f"Max retries exceeded with url: /triggers/T0ABC/123/{TOKEN} (Caused by",
        f"https://discord.com/api/v10/webhooks/123456/{TOKEN}",
        f"Max retries exceeded with url: /api/v10/webhooks/123456/{TOKEN}",
        f"Invalid URL 'discord.com/api/webhooks/123456/{TOKEN}'",
    ],
)
def test_webhook_urls_in_every_shape_are_redacted(text):
    assert TOKEN not in redact_secrets(text)


def test_a_password_in_a_url_without_a_scheme_is_redacted_even_when_cut_short():
    msg = "No connection adapters were found for 'admin:OllamaPw9988@ollama.internal:11434/api'"
    assert "OllamaPw9988" not in redact_secrets(msg)
    # Truncated mid-host, as the provider used to pass it to the breaker.
    assert "OllamaPw9988" not in redact_secrets(msg[:70])


def test_ordinary_text_is_left_alone():
    for text in ("write to a@b.com", "git@github.com:org/repo.git", "at 12:30 UTC", "ssh://git@host/x"):
        assert redact_secrets(text) == text


def test_the_breaker_never_holds_a_secret(caplog):
    from app.ai.circuit_breaker import CircuitBreaker

    cb = CircuitBreaker("ollama", fail_threshold=1)
    with caplog.at_level(logging.WARNING):
        cb.record_failure("No connection adapters were found for 'admin:Pw9988@ollama.internal")
    assert "Pw9988" not in cb._last_failure_reason
    assert "Pw9988" not in caplog.text


def test_a_malformed_mcp_key_entry_logs_no_part_of_it(monkeypatch, caplog):
    from app.mcp import mcp_server

    bare = secrets.token_hex(32)
    monkeypatch.setenv("MCP_API_KEYS", f"ide:{secrets.token_hex(8)},{bare}")
    with caplog.at_level(logging.WARNING):
        mcp_server._mcp_named_keys()
    assert bare[:8] not in caplog.text
    assert "entry 2 of MCP_API_KEYS, 64 characters" in caplog.text


def test_a_non_numeric_app_id_is_not_echoed(monkeypatch):
    from app.core import webhook_security

    pasted = "-----BEGIN RSA PRIVATE KEY-----MIIEsecret"
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "x" * 32)
    monkeypatch.setenv("GITHUB_APP_ID", pasted)
    monkeypatch.setenv("GITHUB_PRIVATE_KEY", "-----BEGIN RSA PRIVATE KEY-----\nA\n-----END")
    with pytest.raises(RuntimeError) as err:
        webhook_security.startup_check()
    assert "MIIEsecret" not in str(err.value)
    assert "must be numeric" in str(err.value)


def test_report_does_not_name_the_redis_host():
    from app.core.analytics import AnalyticsUnavailable
    from app.handlers.comments import reviewer

    exc = AnalyticsUnavailable("Error 111 connecting to red-internal-d3adb33f:6379.")
    with patch("app.core.analytics.format_report_comment", side_effect=exc):
        out = reviewer.cmd_report("o/r")
    assert "red-internal" not in out and "AnalyticsUnavailable" in out


class TestMetricsEndpointsFailClosedInProduction:
    @pytest.fixture
    def srv(self, monkeypatch):
        import server

        monkeypatch.setattr(server, "METRICS_TOKEN", "")
        return server

    @pytest.mark.parametrize("path", ["/health", "/metrics", "/graph.json", "/setup/doctor"])
    def test_closed_without_a_token_in_production(self, srv, monkeypatch, path):
        monkeypatch.setattr(srv, "_PRODUCTION", True)
        r = srv.app.test_client().get(path)
        assert r.status_code == 401
        assert "METRICS_AUTH_TOKEN is not set" in r.get_json()["detail"]

    def test_open_without_a_token_in_development(self, srv, monkeypatch):
        monkeypatch.setattr(srv, "_PRODUCTION", False)
        assert srv.app.test_client().get("/metrics").status_code == 200

    def test_ping_stays_open(self, srv, monkeypatch):
        """Render's health check and the keepalive use /ping."""
        monkeypatch.setattr(srv, "_PRODUCTION", True)
        assert srv.app.test_client().get("/ping").status_code == 200


class TestScannerKnowsTheseShapes:
    def _hits(self, line):
        from app.security.enhanced_secrets import scan_diff

        return [f.pattern_name for f in scan_diff("+" + line)]

    def test_found(self):
        fernet = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        assert "OpenRouter API Key" in self._hits(f'k = "sk-or-v1-{secrets.token_hex(32)}"')
        assert "Slack Webhook" in self._hits(
            f"https://hooks.slack.com/triggers/T0ABC12345/1234567890/{secrets.token_hex(16)}"
        )
        assert "Discord Webhook" in self._hits(
            f"https://discord.com/api/v10/webhooks/123456789012345678/{secrets.token_urlsafe(51)}"
        )
        assert "Fernet Key" in self._hits(f"MEMORY_BACKUP_KEY={fernet}")
        assert "Hex Token" in self._hits(f"API_TOKEN={secrets.token_hex(32)}")

    def test_digests_are_not_secrets(self):
        digest = secrets.token_hex(32)
        assert self._hits(f"    --hash=sha256:{digest}") == []
        assert self._hits(f'digest = "{digest}"') == []
