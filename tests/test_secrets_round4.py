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


def test_a_committed_key_is_not_sent_to_the_model_and_the_finding_still_lands():
    """A credential committed in a PR went to the cloud provider verbatim.
    Masked in the router; grounding applies the same mask, so the model's
    quote of the masked line still anchors the finding."""
    from unittest.mock import MagicMock

    import app.handlers.pull_request as prh
    from app.core.config import Config

    key = "sk-or-v1-" + secrets.token_hex(32)
    files = [{
        "filename": "app/cfg.py", "status": "modified", "additions": 1, "deletions": 0,
        "patch": f'@@ -1,1 +1,2 @@\n import os\n+OPENROUTER_KEY = "{key}"\n',
    }]
    sent = []

    def provider(system, user, **kw):
        sent.append(user)
        return {
            "type": "security", "risk_level": "high", "risk_reason": "key committed",
            "summary": "Adds a key.", "review_focus": [], "confidence": 0.9, "has_gaps": False,
            "files": [{"file": "app/cfg.py", "summary": "Hardcoded key.", "issues": [{
                "severity": "critical", "line": "2", "code": 'OPENROUTER_KEY = "[REDACTED]"',
                "issue": "Hardcoded API key", "fix": 'OPENROUTER_KEY = os.environ["OPENROUTER_KEY"]',
            }]}],
        }, MagicMock(total_tokens=0)

    from app.ai.router import router as real_router

    inline, bodies = [], []

    def ask(system, user, **kw):
        # Through the router's own sanitiser, as a real call would be.
        return provider(system, real_router._sanitize(user, 100_000), **kw)

    with patch.object(prh, "get_installation_token", return_value="t"), \
         patch.object(prh, "load_config", return_value=Config({})), \
         patch.object(prh, "gh_get", return_value={"archived": False}), \
         patch("app.github.helpers.pr_files", return_value=files), \
         patch("app.ai.gatekeeper.is_substantive", return_value=(True, "")), \
         patch("app.ai.router.router.ask", side_effect=ask), \
         patch("app.ai.router.router.ask_text", return_value=("Adds a key.", MagicMock())), \
         patch("app.handlers.pull_request.review._posted_findings", return_value=set()), \
         patch.object(prh, "_post_inline_review",
               side_effect=lambda *a, **k: inline.extend(a[5]) or ""), \
         patch("app.handlers.pull_request.gaps._in_repository_tests", return_value=False), \
         patch.object(prh, "update_sticky_if_present",
                      side_effect=lambda *a: bodies.append(a[4]) or {"id": 1}), \
         patch.object(prh, "upsert_sticky", side_effect=lambda *a: bodies.append(a[4]) or {"id": 1}):
        prh.handle({
            "action": "synchronize", "repository": {"full_name": "o/r"}, "installation": {"id": 1},
            "pull_request": {"number": 7, "title": "cfg", "body": "", "user": {"login": "dev"},
                             "head": {"sha": "abc"}, "base": {"ref": "main"}},
        })
    assert sent, "the model was never asked"
    assert all(key not in u for u in sent)
    assert any("Hardcoded API key" in c["body"] and c["line"] == 2 for c in inline), bodies


def test_autofix_never_commits_the_mask():
    from app.handlers.autofix import _apply_edits

    current = 'import os\nKEY = "real-value-123"\n'
    assert _apply_edits(current, [{"find": "import os\n", "replace": 'import os\nKEY = "[REDACTED]"\n'}]) is None
    assert _apply_edits(current, [{"find": "import os", "replace": "import os, sys"}]) == (
        'import os, sys\nKEY = "real-value-123"\n'
    )
