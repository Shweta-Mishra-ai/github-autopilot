"""
tests/test_integration_e2e.py — End-to-end integration tests through the real
Flask app (`server.app`), not through mocked internals.

WHY THIS FILE EXISTS
  Every other test module unit-tests one module at a time with its immediate
  dependencies mocked. That's necessary but not sufficient: it can't catch a
  wiring bug between server.py, webhook_security, idempotency, thread_pool,
  and a handler. These tests boot the actual Flask app and drive it through
  its real HTTP surface with a real HMAC signature, so they exercise the same
  code path GitHub's webhook delivery does.

  Uses the repo's `_FLASK_MOCKED` convention (see tests/test_dashboard.py) to
  skip cleanly on the runs where an earlier test module has replaced `flask`
  in sys.modules with a MagicMock.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from unittest.mock import MagicMock

import pytest

_FLASK_MOCKED = isinstance(sys.modules.get("flask"), MagicMock)
needs_flask = pytest.mark.skipif(_FLASK_MOCKED, reason="Flask is mocked by another test module")

WEBHOOK_SECRET = "test-webhook-secret-32chars-long!!"  # matches conftest.env_defaults


def _sign(payload: bytes) -> str:
    return "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()


@pytest.fixture()
def srv():
    # Import inside the fixture so conftest's env_defaults applies before
    # server._boot()/startup_check() runs at import time.
    import server

    server.app.config["TESTING"] = True
    return server


@pytest.fixture()
def client(srv):
    return srv.app.test_client()


@needs_flask
class TestWebhookEndToEnd:
    def test_valid_signature_is_accepted_and_dispatched(self, client, monkeypatch):
        # Stub the handler so we assert dispatch happened without touching GitHub.
        called = {}
        monkeypatch.setattr(
            "app.handlers.issues.handle", lambda payload: called.setdefault("ran", payload)
        )

        body = json.dumps(
            {
                "action": "opened",
                "repository": {"full_name": "o/r"},
                "sender": {"login": "a-human", "type": "User"},
                "issue": {"number": 1, "title": "t", "body": "b", "user": {"login": "a-human"}},
            }
        ).encode()

        resp = client.post(
            "/webhook",
            data=body,
            headers={
                "X-Hub-Signature-256": _sign(body),
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "e2e-delivery-1",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 202
        assert resp.get_json()["status"] == "accepted"

        # Dispatch runs in the thread pool — give it a moment.
        for _ in range(20):
            if called.get("ran"):
                break
            time.sleep(0.05)
        assert called.get("ran", {}).get("repository", {}).get("full_name") == "o/r"

    def test_invalid_signature_is_rejected(self, client):
        body = json.dumps({"repository": {"full_name": "o/r"}}).encode()
        resp = client.post(
            "/webhook",
            data=body,
            headers={
                "X-Hub-Signature-256": "sha256=" + "0" * 64,
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "e2e-bad-sig",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 401

    def test_duplicate_delivery_id_is_deduped(self, client, monkeypatch):
        monkeypatch.setattr("app.handlers.issues.handle", lambda payload: None)
        body = json.dumps(
            {
                "action": "opened",
                "repository": {"full_name": "o/r"},
                "sender": {"login": "a-human", "type": "User"},
                "issue": {"number": 2},
            }
        ).encode()
        headers = {
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "e2e-dup-1",
            "Content-Type": "application/json",
        }

        first = client.post("/webhook", data=body, headers=headers)
        second = client.post("/webhook", data=body, headers=headers)
        assert first.status_code == 202
        assert second.status_code == 200
        assert "duplicate" in second.get_json()["status"]

    def test_bot_sender_is_skipped_before_dispatch(self, client, monkeypatch):
        # If this were dispatched, this handler stub would raise and fail the test.
        def _boom(payload):
            raise AssertionError("bot-authored event must never reach a handler")

        monkeypatch.setattr("app.handlers.issues.handle", _boom)
        body = json.dumps(
            {
                "action": "opened",
                "repository": {"full_name": "o/r"},
                "sender": {"login": "some-bot[bot]", "type": "Bot"},
            }
        ).encode()
        resp = client.post(
            "/webhook",
            data=body,
            headers={
                "X-Hub-Signature-256": _sign(body),
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "e2e-bot-1",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200
        assert "bot" in resp.get_json()["status"]
        time.sleep(0.1)  # give a wrongly-dispatched handler a chance to raise

    def test_handler_exception_does_not_crash_pipeline(self, client, monkeypatch):
        """
        A malformed-but-signed payload that makes the handler raise must be
        caught by server._run_handler and logged — never propagate, never
        take down the thread pool for subsequent requests.
        """

        def _raises(payload):
            raise KeyError("simulated handler bug")

        monkeypatch.setattr("app.handlers.issues.handle", _raises)
        body = json.dumps(
            {
                "action": "opened",
                "repository": {"full_name": "o/r"},
                "sender": {"login": "a-human", "type": "User"},
            }
        ).encode()
        resp = client.post(
            "/webhook",
            data=body,
            headers={
                "X-Hub-Signature-256": _sign(body),
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "e2e-crash-1",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 202  # ACKed even though the handler will fail async
        time.sleep(0.1)

        # Pool must still be usable for the next request.
        monkeypatch.setattr("app.handlers.issues.handle", lambda payload: None)
        body2 = json.dumps(
            {
                "action": "opened",
                "repository": {"full_name": "o/r"},
                "sender": {"login": "a-human", "type": "User"},
            }
        ).encode()
        resp2 = client.post(
            "/webhook",
            data=body2,
            headers={
                "X-Hub-Signature-256": _sign(body2),
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "e2e-crash-2",
                "Content-Type": "application/json",
            },
        )
        assert resp2.status_code == 202


@needs_flask
class TestAuthGatedEndpointsEndToEnd:
    def test_health_requires_token_when_configured(self, client, monkeypatch, srv):
        monkeypatch.setattr(srv, "METRICS_TOKEN", "the-real-token")
        assert client.get("/health").status_code == 401
        ok = client.get("/health", headers={"Authorization": "Bearer the-real-token"})
        assert ok.status_code in (200, 207)

    def test_metrics_requires_token_when_configured(self, client, monkeypatch, srv):
        monkeypatch.setattr(srv, "METRICS_TOKEN", "the-real-token")
        assert client.get("/metrics").status_code == 401
        assert (
            client.get("/metrics", headers={"Authorization": "Bearer the-real-token"}).status_code
            == 200
        )

    def test_mcp_fails_closed_without_key(self, client, monkeypatch):
        import app.mcp.mcp_server as mcp_server

        monkeypatch.setattr(mcp_server, "_mcp_api_key", lambda: "")
        resp = client.post("/mcp", json={"method": "tools/list", "params": {}})
        assert resp.status_code == 503

    def test_mcp_tools_list_with_correct_key(self, client, monkeypatch):
        import app.mcp.mcp_server as mcp_server

        monkeypatch.setattr(mcp_server, "_mcp_api_key", lambda: "right-key")
        resp = client.post(
            "/mcp",
            json={"method": "tools/list", "params": {}},
            headers={"Authorization": "Bearer right-key"},
        )
        assert resp.status_code == 200
        assert len(resp.get_json()["tools"]) == 8

    def test_dashboard_serves_without_leaking_configured_token(self, client, monkeypatch, srv):
        monkeypatch.setattr(srv, "METRICS_TOKEN", "super-secret-do-not-leak")
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "super-secret-do-not-leak" not in resp.get_data(as_text=True)
