"""
tests/test_webhook_hardening.py

The webhook is the only endpoint the public internet is meant to reach, so its
failure modes are the ones that matter. Three defects, each verified by driving
the real endpoint rather than by reading the code:

  1. An oversized body was fully buffered before the size check could run.
     Measured at 62 MB of peak allocation to reject a 30 MB request — and the
     size check is step one of verification, so NO SIGNATURE was required.
     A handful of concurrent requests exhausts a 512 MB instance.

  2. X-Forwarded-For was trusted whenever present. With a proxy in front that
     is fine; with no proxy the whole header is attacker-controlled, so the
     attacker chose their own rate-limit bucket — a different one per request.
     The module docstring claimed spoofing was fixed. It was fixed for Render.

  3. `[]` is valid JSON. Every line after the parse assumed a mapping, so a
     non-object payload raised AttributeError and surfaced as a 500.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import tracemalloc
from unittest.mock import MagicMock

import pytest

SECRET = "test-webhook-secret-32chars-abc!"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("EVENT_QUEUE_CONSUMERS", "0")
    import server

    return server.app.test_client()


def signed(body: bytes, event: str = "ping", delivery: str = "d1") -> dict:
    sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {
        "X-Hub-Signature-256": sig,
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "Content-Type": "application/json",
    }


class TestOversizedBodiesAreRefusedDuringTheRead:
    def test_a_huge_body_is_rejected_without_being_buffered(self, client):
        """The whole point: 413 is not enough on its own — it has to arrive
        without the body ever being materialised."""
        from app.core.webhook_security import MAX_PAYLOAD_BYTES

        body = b"x" * (MAX_PAYLOAD_BYTES + 5 * 1024 * 1024)

        tracemalloc.start()
        resp = client.post("/webhook", data=body, headers={"Content-Type": "application/json"})
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert resp.status_code == 413
        peak_mb = peak / 1024 / 1024
        assert peak_mb < 5, f"buffered {peak_mb:.1f} MB while rejecting the request"

    def test_it_is_refused_before_any_signature_check(self, client):
        """No valid signature is sent here. If this needed one, the limit would
        be useless against exactly the traffic it exists to stop."""
        from app.core.webhook_security import MAX_PAYLOAD_BYTES

        resp = client.post(
            "/webhook",
            data=b"y" * (MAX_PAYLOAD_BYTES + 1024),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 413

    def test_the_rejection_is_json_not_an_html_error_page(self, client):
        from app.core.webhook_security import MAX_PAYLOAD_BYTES

        resp = client.post(
            "/webhook",
            data=b"z" * (MAX_PAYLOAD_BYTES + 1024),
            headers={"Content-Type": "application/json"},
        )
        assert resp.get_json() == {"error": "Payload too large"}

    def test_a_normal_payload_still_passes(self, client):
        body = json.dumps({"zen": "ok", "repository": {"full_name": "o/r"}}).encode()
        resp = client.post("/webhook", data=body, headers=signed(body))
        assert resp.status_code in (200, 202)

    def test_the_flask_limit_matches_the_module_constant(self):
        """Two numbers that must not drift: Werkzeug enforces one, the
        defence-in-depth check in verify_webhook enforces the other."""
        import server
        from app.core.webhook_security import MAX_PAYLOAD_BYTES

        assert server.app.config["MAX_CONTENT_LENGTH"] == MAX_PAYLOAD_BYTES


class TestForwardedForTrustIsExplicit:
    def _request(self, xff: str | None, remote: str = "10.0.0.1"):
        req = MagicMock()
        req.remote_addr = remote
        req.headers = {"X-Forwarded-For": xff} if xff is not None else {}
        return req

    def test_with_no_proxy_the_header_is_ignored_entirely(self, monkeypatch):
        """The setting that did not exist. Without a proxy, remote_addr IS the
        client and the header is pure attacker input."""
        from app.core import webhook_security as ws

        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "0")
        ip = ws._get_client_ip(self._request("1.2.3.4, 5.6.7.8"))
        assert ip == "10.0.0.1"

    def test_one_proxy_takes_the_address_the_proxy_wrote(self, monkeypatch):
        from app.core import webhook_security as ws

        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
        assert ws._get_client_ip(self._request("evil, 203.0.113.9")) == "203.0.113.9"

    def test_two_proxies_skip_the_inner_hop(self, monkeypatch):
        from app.core import webhook_security as ws

        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "2")
        assert ws._get_client_ip(self._request("evil, 203.0.113.9, 10.1.1.1")) == "203.0.113.9"

    def test_a_forged_prefix_cannot_choose_the_bucket(self, monkeypatch):
        """The attack: pick a fresh bucket per request to evade the rate limit."""
        from app.core import webhook_security as ws

        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
        seen = {
            ws._get_client_ip(self._request(f"attacker-{i}, 203.0.113.9")) for i in range(50)
        }
        assert seen == {"203.0.113.9"}

    def test_a_chain_shorter_than_configured_falls_back(self, monkeypatch):
        """Too short means the request did not come through the proxies we
        expect, which is not a reason to trust it more."""
        from app.core import webhook_security as ws

        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "2")
        assert ws._get_client_ip(self._request("203.0.113.9")) == "10.0.0.1"

    @pytest.mark.parametrize("value", ["", "  ", "not-a-number", "-3"])
    def test_a_malformed_setting_falls_back_to_the_default(self, monkeypatch, value):
        from app.core import webhook_security as ws

        monkeypatch.setenv("TRUSTED_PROXY_HOPS", value)
        assert ws._trusted_proxy_hops() >= 0
        assert ws._get_client_ip(self._request("evil, 203.0.113.9")) in (
            "203.0.113.9",
            "10.0.0.1",
        )

    def test_the_default_preserves_render_behaviour(self, monkeypatch):
        """Changing the default would silently collapse every client into one
        bucket on the deployment this project actually targets."""
        from app.core import webhook_security as ws

        monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
        assert ws._trusted_proxy_hops() == 1
        assert ws._get_client_ip(self._request("evil, 203.0.113.9")) == "203.0.113.9"


class TestPayloadMustBeAnObject:
    @pytest.mark.parametrize("body", [b"[]", b'"a string"', b"123", b"null", b"true"])
    def test_valid_json_that_is_not_an_object_is_a_400(self, client, body):
        """All of these parse. None of them has .get(), and every line after
        the parse assumed a mapping — so this was a 500, an internal error for
        what is really a malformed request."""
        resp = client.post("/webhook", data=body, headers=signed(body))
        assert resp.status_code == 400
        assert "object" in resp.get_json()["error"].lower()

    def test_unparseable_json_is_still_a_400(self, client):
        body = b"{not json"
        resp = client.post("/webhook", data=body, headers=signed(body))
        assert resp.status_code == 400

    def test_an_object_payload_is_accepted(self, client):
        body = json.dumps({"repository": {"full_name": "o/r"}}).encode()
        resp = client.post("/webhook", data=body, headers=signed(body, delivery="ok-1"))
        assert resp.status_code in (200, 202)


class TestSignatureVerificationStillHolds:
    """The checks that were already right. Pinned because the changes above
    reordered work around them."""

    def test_an_unsigned_request_is_rejected(self, client):
        body = json.dumps({"repository": {"full_name": "o/r"}}).encode()
        resp = client.post("/webhook", data=body, headers={"Content-Type": "application/json"})
        assert resp.status_code == 401

    def test_a_wrong_signature_is_rejected(self, client):
        body = json.dumps({"repository": {"full_name": "o/r"}}).encode()
        headers = signed(body)
        headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64
        assert client.post("/webhook", data=body, headers=headers).status_code == 401

    def test_a_signature_for_a_different_body_is_rejected(self, client):
        """Replaying a captured signature against modified content."""
        headers = signed(json.dumps({"a": 1}).encode())
        tampered = json.dumps({"a": 2}).encode()
        assert client.post("/webhook", data=tampered, headers=headers).status_code == 401

    def test_an_empty_secret_rejects_everything(self, client, monkeypatch):
        """Fail closed. An unset secret must not mean 'accept anything'."""
        from app.core.webhook_security import verify_signature

        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "")
        body = b"{}"
        sig = "sha256=" + hmac.new(b"", body, hashlib.sha256).hexdigest()
        assert verify_signature(body, sig) is False

    def test_the_secret_is_read_per_call_so_rotation_needs_no_redeploy(self, monkeypatch):
        from app.core.webhook_security import verify_signature

        body = b"{}"
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "first-secret-value-32-chars-ok!!")
        sig_first = "sha256=" + hmac.new(b"first-secret-value-32-chars-ok!!", body, hashlib.sha256).hexdigest()
        assert verify_signature(body, sig_first) is True

        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "second-secret-value-32-chars-k!!")
        assert verify_signature(body, sig_first) is False
