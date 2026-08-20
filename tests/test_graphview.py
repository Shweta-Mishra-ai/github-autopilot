"""
tests/test_graphview.py

Route tests for the codebase-map view (GET /graph) and its data endpoint
(GET /graph.json).

The property that matters most: /graph.json is auth-gated the same way /health
is. A dependency graph is a map of the codebase -- every module name, its size,
and what depends on what -- so serving it publicly on a private deployment
would leak the shape of the whole system.
"""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

# Same convention as test_dashboard.py: other test modules mock `flask` in
# sys.modules at collection time, and server is imported inside a fixture so
# conftest's env defaults are applied before _boot() runs startup_check().
_FLASK_MOCKED = isinstance(sys.modules.get("flask"), MagicMock)
needs_flask = pytest.mark.skipif(
    _FLASK_MOCKED, reason="Flask is mocked by another test module"
)


@pytest.fixture
def srv():
    import server

    server.app.config["TESTING"] = True
    return server


@pytest.fixture
def client(srv):
    with srv.app.test_client() as c:
        yield c


@pytest.fixture
def graph_file(tmp_path):
    payload = {
        "nodes": [
            {
                "id": "app.core.config",
                "path": "app/core/config.py",
                "layer": "core",
                "loc": 359,
                "functions": 8,
                "classes": 1,
                "is_package": False,
                "fan_in": 6,
                "fan_out": 1,
                "external_deps": ["yaml"],
            }
        ],
        "edges": [],
        "stats": {"modules": 1, "edges": 0, "total_loc": 359, "cycles": [], "hotspots": []},
    }
    p = tmp_path / "codegraph.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


@needs_flask
class TestGraphPage:
    def test_serves_html(self, client):
        r = client.get("/graph")
        assert r.status_code == 200
        assert "text/html" in r.headers["Content-Type"]

    def test_page_is_not_auth_gated(self, srv, client):
        """The shell holds no data and no secret -- same as /dashboard. The
        token gate belongs on /graph.json, which is what carries the map."""
        with patch.object(srv, "METRICS_TOKEN", "a-real-token"):
            assert client.get("/graph").status_code == 200

    def test_page_contains_no_secret(self, srv, client):
        with patch.object(srv, "METRICS_TOKEN", "super-secret-value"):
            assert b"super-secret-value" not in client.get("/graph").data

    def test_page_references_its_data_endpoint(self, client):
        assert b"/graph.json" in client.get("/graph").data

    def test_page_is_noindex(self, client):
        assert b"noindex" in client.get("/graph").data


@needs_flask
class TestGraphJson:
    def test_serves_the_generated_file(self, srv, client, graph_file, monkeypatch):
        monkeypatch.setenv("CODEGRAPH_PATH", str(graph_file))
        with patch.object(srv, "METRICS_TOKEN", ""):
            r = client.get("/graph.json")
        assert r.status_code == 200
        assert r.get_json()["stats"]["modules"] == 1

    def test_content_type_is_json(self, srv, client, graph_file, monkeypatch):
        monkeypatch.setenv("CODEGRAPH_PATH", str(graph_file))
        with patch.object(srv, "METRICS_TOKEN", ""):
            r = client.get("/graph.json")
        assert "application/json" in r.headers["Content-Type"]

    def test_requires_the_metrics_token_when_set(self, srv, client, graph_file, monkeypatch):
        monkeypatch.setenv("CODEGRAPH_PATH", str(graph_file))
        with patch.object(srv, "METRICS_TOKEN", "a-real-token"):
            assert client.get("/graph.json").status_code == 401

    def test_accepts_the_correct_token(self, srv, client, graph_file, monkeypatch):
        monkeypatch.setenv("CODEGRAPH_PATH", str(graph_file))
        with patch.object(srv, "METRICS_TOKEN", "a-real-token"):
            r = client.get(
                "/graph.json", headers={"Authorization": "Bearer a-real-token"}
            )
        assert r.status_code == 200

    def test_rejects_a_wrong_token(self, srv, client, graph_file, monkeypatch):
        monkeypatch.setenv("CODEGRAPH_PATH", str(graph_file))
        with patch.object(srv, "METRICS_TOKEN", "a-real-token"):
            r = client.get("/graph.json", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    def test_missing_graph_returns_404_with_the_command_to_generate_it(
        self, srv, client, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("CODEGRAPH_PATH", str(tmp_path / "nope.json"))
        with patch.object(srv, "METRICS_TOKEN", ""):
            r = client.get("/graph.json")
        assert r.status_code == 404
        assert "codegraph" in r.get_json()["hint"]

    def test_unreadable_graph_returns_500_not_a_traceback(
        self, srv, client, graph_file, monkeypatch
    ):
        monkeypatch.setenv("CODEGRAPH_PATH", str(graph_file))
        with patch.object(srv, "METRICS_TOKEN", ""), patch(
            "builtins.open", side_effect=OSError("disk error")
        ):
            r = client.get("/graph.json")
        assert r.status_code == 500
        assert "disk error" not in r.get_data(as_text=True)


class TestGraphHtmlContent:
    def test_is_self_contained(self):
        """No CDN: the Render free tier has no build step, and an external
        script tag is the first thing a strict CSP blocks."""
        from app.graphview import graph_html

        html = graph_html()
        assert "https://" not in html.split("<script>")[1] if "<script>" in html else True
        assert "<script src=" not in html
        assert "<link rel=\"stylesheet\"" not in html

    def test_escapes_untrusted_values(self):
        """Module ids and paths come from the scanned repository and are
        injected into innerHTML."""
        from app.graphview import graph_html

        assert "function esc(" in graph_html()
