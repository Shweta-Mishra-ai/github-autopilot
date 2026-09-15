"""
tests/test_graph_svg.py

The committed picture of the dependency graph — docs/diagrams/codegraph.svg.

Why this file has more tests than the renderer has features: this SVG is the
only view of the graph that a reader can reach with no deployment, no auth
token and no JavaScript, and it is embedded in the README, which is the most
read file in the repository. A picture that silently renders blank, or renders
stale, is worse than no picture — it argues that the structure it shows is
current.

Three classes of property are pinned here:

  1. It renders at all, and renders the data it was given.
  2. It stays inside what GitHub will actually display: an <img>-embedded SVG
     executes no script and fetches nothing, so anything needing either is
     invisible rather than broken-looking.
  3. It is byte-reproducible, because CI diffs the committed file.
"""

from __future__ import annotations

import json
import pathlib
import re
import xml.etree.ElementTree as ET

import pytest

from app.intelligence.graph_svg import LAYER_COLORS, render_svg

SVG_PATH = pathlib.Path("docs/diagrams/codegraph.svg")
JSON_PATH = pathlib.Path("docs/diagrams/codegraph.json")


SVG_NS = "{http://www.w3.org/2000/svg}"


def _import_curves(root):
    """Only the edge curves.

    These assertions used to count every <path> in the document, which was the
    right number only while imports were the one curved thing in it. Adding the
    layer bands — also paths — broke both of them without either the renderer or
    the property under test being wrong. Ask the group by name instead.
    """
    group = root.find(f'{SVG_NS}g/{SVG_NS}g[@id="imports"]')
    if group is None:
        group = next(
            (g for g in root.iter(f"{SVG_NS}g") if g.get("id") == "imports"), None
        )
    assert group is not None, "the renderer no longer emits an #imports group"
    return list(group.iter(f"{SVG_NS}path"))


def _payload(**over) -> dict:
    base = {
        "nodes": [
            {
                "id": "app.core.config",
                "path": "app/core/config.py",
                "layer": "core",
                "loc": 391,
                "functions": 8,
                "classes": 1,
                "is_package": False,
                "fan_in": 7,
                "fan_out": 2,
                "external_deps": ["yaml"],
            },
            {
                "id": "app.handlers.push",
                "path": "app/handlers/push.py",
                "layer": "handlers",
                "loc": 523,
                "functions": 12,
                "classes": 0,
                "is_package": False,
                "fan_in": 1,
                "fan_out": 6,
                "external_deps": [],
            },
            {
                "id": "app.ai.router",
                "path": "app/ai/router.py",
                "layer": "ai",
                "loc": 688,
                "functions": 20,
                "classes": 1,
                "is_package": False,
                "fan_in": 13,
                "fan_out": 4,
                "external_deps": [],
            },
        ],
        "edges": [
            {"source": "app.handlers.push", "target": "app.core.config", "kind": "import"},
            {"source": "app.handlers.push", "target": "app.ai.router", "kind": "runtime"},
        ],
        "stats": {
            "modules": 3,
            "edges": 2,
            "total_loc": 1602,
            "layers": ["ai", "core", "handlers"],
            "cycles": [],
            "hotspots": [{"id": "app.ai.router", "loc": 688, "fan_in": 13, "fan_out": 4}],
            "orphans": [],
        },
    }
    base.update(over)
    return base


# ── It renders, and renders what it was given ────────────────────────────────


class TestRenders:
    def test_output_is_well_formed_xml(self):
        ET.fromstring(render_svg(_payload()))

    def test_root_is_an_svg_with_a_viewbox(self):
        root = ET.fromstring(render_svg(_payload()))
        assert root.tag.endswith("svg")
        assert root.get("viewBox")

    def test_one_dot_per_module(self):
        svg = render_svg(_payload())
        root = ET.fromstring(svg)
        circles = root.iter("{http://www.w3.org/2000/svg}circle")
        # Legend swatches are circles too, and there is one per layer present.
        assert len(list(circles)) == 3 + 3

    def test_one_curve_per_import(self):
        root = ET.fromstring(render_svg(_payload()))
        assert len(_import_curves(root)) == 2

    def test_a_runtime_import_is_drawn_differently_from_a_top_level_one(self):
        """A deferred import is a weaker coupling. Drawing both the same way
        overstates how tangled the codebase is."""
        root = ET.fromstring(render_svg(_payload()))
        dashed = [p for p in _import_curves(root) if p.get("stroke-dasharray")]
        assert len(dashed) == 1

    def test_module_names_appear_as_text(self):
        svg = render_svg(_payload())
        assert ">router<" in svg
        assert ">config<" in svg
        assert ">push<" in svg

    def test_the_headline_numbers_are_drawn(self):
        svg = render_svg(_payload())
        assert "Modules" in svg and ">3<" in svg
        assert "1,602" in svg  # total_loc, thousands-separated

    def test_each_layer_keeps_its_own_colour(self):
        svg = render_svg(_payload())
        for layer in ("core", "handlers", "ai"):
            assert LAYER_COLORS[layer] in svg

    def test_the_palette_matches_the_interactive_map(self):
        """Two views of one dataset. A module that is pink on the page and
        green in the picture reads as two different systems."""
        from app.graphview import graph_html

        html = graph_html()
        for layer, colour in LAYER_COLORS.items():
            assert colour in html, f"{layer} colour {colour} missing from /graph"


class TestReportsProblems:
    def test_a_clean_graph_says_so(self):
        svg = render_svg(_payload())
        assert "Import cycles" in svg
        assert svg.count(">none<") >= 2

    def test_an_import_cycle_is_named_in_the_picture(self):
        p = _payload()
        p["stats"]["cycles"] = [["app.a", "app.b"]]
        svg = render_svg(p)
        assert "IMPORT CYCLES" in svg
        assert "app.a → app.b" in svg

    def test_an_unreferenced_module_is_named_in_the_picture(self):
        p = _payload()
        p["stats"]["orphans"] = ["app.core.forgotten"]
        svg = render_svg(p)
        assert "NOTHING IMPORTS THESE" in svg
        assert "app.core.forgotten" in svg


# ── What GitHub will actually display ────────────────────────────────────────


class TestStaysRenderable:
    """An SVG in a README is loaded inside an <img>. That context executes no
    script and performs no fetch, so a renderer that reaches for either
    produces a picture that is silently missing pieces."""

    def test_contains_no_script(self):
        svg = render_svg(_payload())
        assert "<script" not in svg.lower()
        assert "javascript:" not in svg.lower()

    def test_makes_no_external_request(self):
        # The xmlns value is a namespace *identifier*, never fetched, and is
        # the one URL that has to be in the document.
        svg = render_svg(_payload()).replace('xmlns="http://www.w3.org/2000/svg"', "")
        for token in ("http://", "https://", "//fonts.", "@import", "xlink:href", "<image"):
            assert token not in svg, f"external reference: {token}"

    def test_declares_the_svg_namespace(self):
        assert 'xmlns="http://www.w3.org/2000/svg"' in render_svg(_payload())

    def test_uses_only_generic_font_families(self):
        """A web font cannot load in this context, and a missing font falls
        back to a metric the layout was not built for."""
        svg = render_svg(_payload())
        assert "font-family" in svg
        assert "url(" not in svg

    def test_has_a_text_alternative(self):
        svg = render_svg(_payload())
        assert 'role="img"' in svg
        assert "aria-label=" in svg
        assert "<title>" in svg and "<desc>" in svg

    def test_paints_its_own_background(self):
        """GitHub shows it on white in light mode and near-black in dark mode.
        A transparent background makes one of those unreadable."""
        assert '<rect width="1320" height="840" fill="#0b1120"/>' in render_svg(_payload())


class TestEscapesUntrustedText:
    """Module ids and paths come from whatever repository was scanned. They are
    written straight into the document, so the same escaping duty applies here
    as in the HTML page."""

    def test_a_module_name_cannot_close_its_own_element(self):
        p = _payload()
        p["nodes"][0]["id"] = 'app.core.</text><script>alert("x")</script>'
        svg = render_svg(p)
        assert "<script>" not in svg
        ET.fromstring(svg)

    def test_a_cycle_entry_is_escaped(self):
        p = _payload()
        p["stats"]["cycles"] = [["<img src=x onerror=y>", "app.b"]]
        svg = render_svg(p)
        assert "<img src=x" not in svg
        ET.fromstring(svg)

    def test_an_orphan_entry_is_escaped(self):
        p = _payload()
        p["stats"]["orphans"] = ["a&b<c>"]
        svg = render_svg(p)
        assert "&amp;" in svg
        ET.fromstring(svg)


# ── Degenerate input ─────────────────────────────────────────────────────────


class TestSurvivesOddInput:
    def test_an_empty_graph_still_produces_a_document(self):
        svg = render_svg({"nodes": [], "edges": [], "stats": {}})
        ET.fromstring(svg)
        assert "<svg" in svg

    def test_a_missing_stats_block_does_not_raise(self):
        ET.fromstring(render_svg({"nodes": _payload()["nodes"], "edges": []}))

    def test_a_single_module_does_not_divide_by_zero(self):
        p = _payload()
        p["nodes"] = p["nodes"][:1]
        p["edges"] = []
        ET.fromstring(render_svg(p))

    def test_an_unknown_layer_gets_the_default_colour(self):
        p = _payload()
        p["nodes"][0]["layer"] = "something-new"
        svg = render_svg(p)
        ET.fromstring(svg)
        assert "something-new" in svg  # named in the legend rather than dropped

    def test_an_edge_to_a_module_that_is_not_in_the_graph_is_skipped(self):
        """finalise() drops these, but a hand-edited or truncated payload can
        still carry one. A curve to a node with no coordinates is drawn to
        (undefined, undefined), which vanishes without an error."""
        p = _payload()
        p["edges"].append({"source": "app.handlers.push", "target": "app.gone", "kind": "import"})
        root = ET.fromstring(render_svg(p))
        assert len(_import_curves(root)) == 2

    def test_a_module_with_zero_lines_still_gets_a_visible_dot(self):
        p = _payload()
        p["nodes"][0]["loc"] = 0
        root = ET.fromstring(render_svg(p))
        radii = [
            float(c.get("r"))
            for c in root.iter("{http://www.w3.org/2000/svg}circle")
            if c.get("r")
        ]
        assert min(radii) > 0


# ── Reproducibility ──────────────────────────────────────────────────────────


class TestIsReproducible:
    """CI regenerates this file and fails a PR whose committed copy differs. A
    renderer that is not byte-stable turns that gate into a coin flip."""

    def test_rendering_twice_gives_identical_bytes(self):
        p = _payload()
        assert render_svg(p) == render_svg(p)

    def test_node_order_in_the_input_does_not_change_the_output(self):
        p = _payload()
        reversed_p = _payload()
        reversed_p["nodes"] = list(reversed(reversed_p["nodes"]))
        assert render_svg(p) == render_svg(reversed_p)

    def test_every_coordinate_is_rounded(self):
        """Trig can differ in the last bits between libm builds. One decimal
        place is far coarser than that, and far finer than a pixel."""
        svg = render_svg(_payload())
        for value in re.findall(r'c[xy]="([-\d.]+)"', svg):
            _, _, frac = value.partition(".")
            assert len(frac) <= 2, f"unrounded coordinate {value}"


# ── The committed file ───────────────────────────────────────────────────────


@pytest.mark.skipif(not JSON_PATH.exists(), reason="graph data not generated")
class TestCommittedPicture:
    def test_the_svg_is_committed(self):
        assert SVG_PATH.exists(), (
            "docs/diagrams/codegraph.svg is missing. The README embeds it, so "
            "its absence renders as a broken image. Regenerate with:\n"
            "  python -m app.intelligence.codegraph app server.py worker.py "
            "--svg docs/diagrams/codegraph.svg"
        )

    def test_the_committed_svg_matches_the_committed_data(self):
        """The staleness gate, run locally as well as in CI. A picture that no
        longer matches the code is the exact failure this whole approach exists
        to prevent."""
        payload = json.loads(JSON_PATH.read_text(encoding="utf-8"))
        assert SVG_PATH.read_text(encoding="utf-8") == render_svg(payload), (
            "docs/diagrams/codegraph.svg is out of date. Regenerate with:\n"
            "  python -m app.intelligence.codegraph app server.py worker.py \\\n"
            "    --entrypoint app --entrypoint server --entrypoint worker \\\n"
            "    --entrypoint app.dashboard --entrypoint app.graphview \\\n"
            "    --out docs/diagrams/codegraph.json --svg docs/diagrams/codegraph.svg"
        )

    def test_the_committed_svg_is_well_formed(self):
        ET.fromstring(SVG_PATH.read_text(encoding="utf-8"))

    def test_the_committed_svg_is_small_enough_for_a_readme(self):
        size = SVG_PATH.stat().st_size
        assert size < 1_500_000, f"codegraph.svg is {size:,} bytes"

    def test_the_readme_embeds_it(self):
        readme = pathlib.Path("README.md").read_text(encoding="utf-8")
        assert "docs/diagrams/codegraph.svg" in readme, (
            "the README no longer embeds the generated map — which is the only "
            "place most readers will ever see it"
        )


class TestLayerBands:
    """The ring groups modules by layer, and before this the grouping was
    carried entirely by dot colour — eight palettes across 94 dots, which a
    reader decodes rather than sees. A band outside the dots marks where each
    layer starts and ends."""

    def _bands(self, payload=None):
        root = ET.fromstring(render_svg(payload or _payload()))
        group = next((g for g in root.iter(f"{SVG_NS}g") if g.get("id") == "layer-bands"), None)
        assert group is not None, "no layer-bands group"
        return list(group.iter(f"{SVG_NS}path"))

    def test_one_band_per_layer_present(self):
        payload = _payload()
        layers = {n["layer"] for n in payload["nodes"]}
        assert len(self._bands(payload)) == len(layers)

    def test_a_band_takes_its_layers_colour(self):
        colours = {b.get("stroke") for b in self._bands()}
        assert colours <= set(LAYER_COLORS.values()) | {"#9ca3af"}

    def test_a_single_module_layer_still_draws_a_visible_band(self):
        """A run of one has zero angular extent, so an unpadded arc is a path
        from a point to itself — which renders as nothing at all."""
        payload = _payload()
        payload["nodes"] = [payload["nodes"][0]]
        payload["edges"] = []

        d = self._bands(payload)[0].get("d")
        head = re.match(r"M([\d.-]+) ([\d.-]+)A[\d.]+ [\d.]+ 0 \d 1 ([\d.-]+) ([\d.-]+)", d)
        assert head, d
        x0, y0, x1, y1 = (float(v) for v in head.groups())
        assert (x0, y0) != (x1, y1), "a one-module layer drew a zero-length arc"

    def test_bands_sit_outside_the_dots_and_inside_the_labels(self):
        """Overlapping the dots would hide data; overlapping the labels would
        make them unreadable."""
        from app.intelligence.graph_svg import ARC_R, LABEL_R, RING_R

        assert RING_R < ARC_R < LABEL_R


class TestBetweenLayers:
    """The chord diagram draws all 292 imports and lets you trace none of them.
    'Does core reach into handlers?' is unanswerable from the picture, so the
    panel answers it in numbers."""

    def test_imports_inside_a_layer_are_excluded(self):
        """They are the majority and they are expected. Counting them would
        bury the rows worth reading."""
        from app.intelligence.graph_svg import layer_flows

        nodes = [
            {"id": "a", "layer": "core"},
            {"id": "b", "layer": "core"},
            {"id": "c", "layer": "handlers"},
        ]
        edges = [
            {"source": "a", "target": "b"},
            {"source": "c", "target": "a"},
        ]
        assert layer_flows(nodes, edges) == [("handlers", "core", 1)]

    def test_flows_are_counted_and_sorted_by_weight(self):
        from app.intelligence.graph_svg import layer_flows

        nodes = [
            {"id": "h1", "layer": "handlers"},
            {"id": "c1", "layer": "core"},
            {"id": "g1", "layer": "github"},
        ]
        edges = [
            {"source": "h1", "target": "c1"},
            {"source": "h1", "target": "c1"},
            {"source": "h1", "target": "g1"},
        ]
        assert layer_flows(nodes, edges) == [
            ("handlers", "core", 2),
            ("handlers", "github", 1),
        ]

    def test_direction_is_preserved(self):
        """core → github and github → core are different facts, and only one of
        them is a surprise."""
        from app.intelligence.graph_svg import layer_flows

        nodes = [{"id": "c", "layer": "core"}, {"id": "g", "layer": "github"}]
        flows = layer_flows(nodes, [{"source": "c", "target": "g"}])
        assert flows == [("core", "github", 1)]

    def test_equal_counts_order_deterministically(self):
        """The committed SVG is diffed by CI, so a tie that reorders between
        runs would fail a PR that changed nothing."""
        from app.intelligence.graph_svg import layer_flows

        nodes = [
            {"id": "h", "layer": "handlers"},
            {"id": "c", "layer": "core"},
            {"id": "g", "layer": "github"},
            {"id": "a", "layer": "ai"},
        ]
        edges = [{"source": "h", "target": "c"}, {"source": "a", "target": "g"}]
        assert layer_flows(nodes, edges) == [("ai", "github", 1), ("handlers", "core", 1)]

    def test_an_edge_naming_an_unknown_module_is_ignored(self):
        from app.intelligence.graph_svg import layer_flows

        nodes = [{"id": "a", "layer": "core"}]
        assert layer_flows(nodes, [{"source": "a", "target": "missing"}]) == []

    def test_the_section_appears_in_the_rendered_panel(self):
        svg = render_svg(_payload())
        assert "BETWEEN LAYERS" in svg

    def test_no_section_when_nothing_crosses_a_boundary(self):
        """A heading over an empty list is noise."""
        payload = _payload()
        for n in payload["nodes"]:
            n["layer"] = "core"
        assert "BETWEEN LAYERS" not in render_svg(payload)
