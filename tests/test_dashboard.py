"""tests/test_dashboard.py — /dashboard route + HTML shell."""

import sys
from unittest.mock import MagicMock

import pytest

# test_mcp.py / test_commands_fixed.py mock `flask` in sys.modules at collection
# time. When that happens, a real Flask test client is unavailable — follow the
# repo convention and skip the route tests (the pure-HTML test still runs).
_FLASK_MOCKED = isinstance(sys.modules.get("flask"), MagicMock)
needs_flask = pytest.mark.skipif(_FLASK_MOCKED, reason="Flask is mocked by another test module")


@pytest.fixture()
def srv():
    # Import inside the fixture so conftest's env_defaults is applied before
    # server._boot() runs startup_check() at import time.
    import server

    server.app.config["TESTING"] = True
    return server


@pytest.fixture()
def client(srv):
    return srv.app.test_client()


class TestDashboard:
    @needs_flask
    def test_route_serves_html(self, client):
        r = client.get("/dashboard")
        assert r.status_code == 200
        assert "text/html" in r.content_type
        body = r.get_data(as_text=True)
        assert "GitHub Autopilot" in body
        assert "/health" in body  # polls the health endpoint (which embeds metrics)

    @needs_flask
    def test_shell_contains_no_secret(self, client, srv, monkeypatch):
        # Even when a token is configured, the HTML must not embed it.
        monkeypatch.setattr(srv, "METRICS_TOKEN", "super-secret-token-value")
        body = client.get("/dashboard").get_data(as_text=True)
        assert "super-secret-token-value" not in body

    def test_html_builder_is_pure(self):
        from app.dashboard import dashboard_html

        assert dashboard_html() == dashboard_html()
        assert "sessionStorage" in dashboard_html()  # token kept client-side only
        assert "GitHub Autopilot" in dashboard_html()
