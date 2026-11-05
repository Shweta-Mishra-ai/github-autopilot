"""Round-3 audit C6: a non-ASCII credential was a 500, not a refusal."""

import pytest


def test_metrics_auth_refuses_a_non_ascii_bearer(monkeypatch):
    import server

    monkeypatch.setattr(server, "METRICS_TOKEN", "tok")
    c = server.app.test_client()
    for path in ("/health", "/metrics"):
        assert c.get(path, headers={"Authorization": "Bearer é"}).status_code == 401


def test_metrics_auth_still_accepts_the_token(monkeypatch):
    import server

    monkeypatch.setattr(server, "METRICS_TOKEN", "tok")
    assert server._authorized(type("R", (), {"headers": {"Authorization": "Bearer tok"}})())


@pytest.mark.parametrize("header", ["sha256=é", "sha256=" + "ü" * 64])
def test_webhook_signature_refuses_non_ascii(header, monkeypatch):
    from app.core import webhook_security

    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "x" * 32)
    assert webhook_security.verify_signature(b"{}", header) is False
