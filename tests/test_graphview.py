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


# ── The contract between the generator and the renderer ───────────────────────


class TestPayloadContract:
    """
    codegraph.py writes the JSON; graphview.py's JavaScript reads it. Nothing
    checks that they agree on field names, and nothing can: a rename on the
    Python side produces an empty canvas, not an exception. No test in this
    file would have failed, and no log line would say why.

    This is the same "produced but never consumed" bug class the validator
    tests guard, except across a language boundary — which is exactly where it
    is least likely to be noticed.
    """

    import json as _json
    import pathlib as _pathlib

    GRAPH = _json.loads(
        _pathlib.Path("docs/diagrams/codegraph.json").read_text(encoding="utf-8")
    )

    # Field names the renderer dereferences off graph/node/edge objects.
    RENDERER_READS_TOP = {"nodes", "edges", "stats"}
    RENDERER_READS_NODE = {"id", "layer", "loc"}
    RENDERER_READS_EDGE = {"source", "target", "kind"}
    RENDERER_READS_STATS = {"hotspots", "orphans"}

    def test_the_generator_emits_every_top_level_key_the_page_reads(self):
        assert set(self.GRAPH) >= self.RENDERER_READS_TOP

    def test_every_node_carries_the_fields_the_page_draws_with(self):
        assert self.GRAPH["nodes"], "no nodes to check"
        for node in self.GRAPH["nodes"]:
            missing = self.RENDERER_READS_NODE - set(node)
            assert not missing, f"node {node.get('id')} missing {missing}"

    def test_every_edge_carries_the_fields_the_page_draws_with(self):
        assert self.GRAPH["edges"], "no edges to check"
        for edge in self.GRAPH["edges"]:
            assert set(edge) >= self.RENDERER_READS_EDGE, edge

    def test_the_side_panels_have_data_to_render(self):
        assert set(self.GRAPH["stats"]) >= self.RENDERER_READS_STATS

    def test_every_edge_endpoint_resolves_to_a_node(self):
        """A dangling endpoint draws a line to coordinates that do not exist.
        In canvas that is not an error — it is a line to (undefined, undefined),
        which silently vanishes, so the picture is quietly wrong."""
        ids = {n["id"] for n in self.GRAPH["nodes"]}
        dangling = [
            f"{e['source']}->{e['target']}"
            for e in self.GRAPH["edges"]
            if e["source"] not in ids or e["target"] not in ids
        ]
        assert dangling == [], f"edges referencing unknown nodes: {dangling[:5]}"

    def test_hotspots_reference_real_modules(self):
        ids = {n["id"] for n in self.GRAPH["nodes"]}
        for h in self.GRAPH["stats"]["hotspots"]:
            assert h["id"] in ids

    def test_the_javascript_reads_no_field_the_generator_does_not_emit(self):
        """Derived from the shipped page rather than from this test's own list,
        so adding a `d.foo` in the JS without adding `foo` in Python fails
        here instead of rendering blank."""
        import re

        from app.graphview import graph_html

        emitted = (
            set(self.GRAPH)
            | set(self.GRAPH["nodes"][0])
            | set(self.GRAPH["edges"][0])
            | set(self.GRAPH["stats"])
        )
        # Locals the layout uses that are not payload fields.
        allowed = emitted | {
            "x", "y", "vx", "vy", "r", "s", "t", "add", "json", "length", "push",
            "forEach", "map", "filter", "textContent", "style", "value", "width",
            "height", "getContext", "toFixed", "sort", "slice", "join", "has",
            "get", "set", "size", "keys", "values", "includes", "toLowerCase",
        }
        reads = set(
            re.findall(r"\b(?:d|n|e|node|edge|link|g|data|graph|stats)\.([a-zA-Z_]\w*)\b", graph_html())
        )
        unknown = reads - allowed
        assert unknown == set(), (
            f"the page reads fields the generator never emits: {sorted(unknown)}. "
            "A rename on the Python side renders an empty canvas, not an error."
        )


class TestPayloadStaysBrowserSized:
    """The whole graph is sent to the browser in one response and laid out with
    a per-frame O(n²) force simulation. Growth here is felt as a page that
    stops being interactive, not as a failure."""

    def test_the_served_payload_is_small_enough_to_ship(self):
        import pathlib

        size = pathlib.Path("docs/diagrams/codegraph.json").stat().st_size
        assert size < 2_000_000, f"codegraph.json is {size:,} bytes"

    def test_node_count_is_within_what_the_layout_can_animate(self):
        import json
        import pathlib

        graph = json.loads(pathlib.Path("docs/diagrams/codegraph.json").read_text(encoding="utf-8"))
        # n² at 60fps stops being smooth well before this; the guard exists so
        # the number is a decision rather than a surprise.
        assert len(graph["nodes"]) < 500, (
            f"{len(graph['nodes'])} nodes — the O(n²) layout needs revisiting "
            "(spatial hashing or edge bundling) before this grows further"
        )
