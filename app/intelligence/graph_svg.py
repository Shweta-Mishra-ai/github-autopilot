"""
app/intelligence/graph_svg.py
Render the dependency graph as a standalone SVG picture.

Why this exists
───────────────
The interactive map at `/graph` can only be seen by someone who (a) has a
running deployment, (b) holds `METRICS_AUTH_TOKEN`, and (c) has JavaScript.
That is three preconditions between a reader and the one diagram in this
project that is derived from the code rather than drawn by hand — so in
practice nobody ever saw it, and the repository's only always-visible
"architecture graph" was an eight-box mermaid block.

This renders the same data as a flat SVG file with no script, no fetch and no
auth. It is committed to the repository and embedded in the README, so it shows
up on the GitHub project page, in a fork, in an offline clone, and in a PDF.
CI regenerates it on every run, so it cannot drift from the code.

Layout: a circular (chord) diagram. Modules sit on a ring grouped by layer;
imports are drawn as quadratic curves bundled through the centre. A ring is the
right shape here for the same reason a force layout is right on the interactive
page and a tree is wrong on both — imports form a general graph, not a
hierarchy — and unlike a force layout it is fully deterministic, which is what
lets CI diff the committed file.

Pure stdlib (math + html only). The CI job that regenerates this installs no
dependencies, and this module must never import the code it draws.
"""

from __future__ import annotations

import math
from html import escape

# ── Appearance ───────────────────────────────────────────────────────────────
# Deliberately the same palette as app/graphview.py: the static picture and the
# interactive map are two views of one dataset, and a module that is pink in one
# and green in the other reads as two different systems.
LAYER_COLORS: dict[str, str] = {
    "handlers": "#22d3ee",
    "core": "#818cf8",
    "ai": "#f472b6",
    "github": "#34d399",
    "security": "#f59e0b",
    "intelligence": "#a78bfa",
    "mcp": "#60a5fa",
    "tests": "#6b7280",
    "evals": "#6b7280",
    "other": "#9ca3af",
}
DEFAULT_COLOR = "#9ca3af"

# Ring order. Fixed rather than alphabetical so the picture reads roughly in the
# direction requests flow (entrypoint → handlers → ai/github → storage), and so
# adding a layer does not reshuffle every existing one.
LAYER_ORDER: tuple[str, ...] = (
    "handlers",
    "ai",
    "intelligence",
    "core",
    "security",
    "github",
    "mcp",
    "other",
    "tests",
    "evals",
)

BG = "#0b1120"
PANEL = "#111827"
BORDER = "#1f2937"
TEXT = "#e5e7eb"
DIM = "#9ca3af"
OK = "#22c55e"
BAD = "#ef4444"

WIDTH = 1320
HEIGHT = 840
CX, CY = 412.0, 420.0
RING_R = 268.0  # radius the module dots sit on
LABEL_R = 277.0  # radius module labels start at
PANEL_X = 800.0

# How hard edges are pulled toward the centre. 0 draws straight chords (an
# unreadable hairball at this density); 1 collapses every edge onto the centre
# point. 0.30 keeps bundles distinguishable while still showing direction.
BUNDLE = 0.30

MAX_LABEL_CHARS = 16


def _color(layer: str) -> str:
    return LAYER_COLORS.get(layer, DEFAULT_COLOR)


def _layer_sort_key(layer: str) -> tuple[int, str]:
    """Known layers in LAYER_ORDER; anything new lands after them, alphabetically."""
    try:
        return (LAYER_ORDER.index(layer), "")
    except ValueError:
        return (len(LAYER_ORDER), layer)


def _r(value: float) -> float:
    """
    Round a coordinate before it reaches the file.

    Not cosmetic. This file is committed and CI fails a PR whose copy is stale,
    so the renderer has to be byte-reproducible. Trig results can differ in the
    last bits between libm builds; one decimal place is far coarser than that
    and still far finer than a pixel.
    """
    return round(value, 1)


def _node_radius(loc: int) -> float:
    """Dot size by module length. sqrt so a 500-line module is not 80x a 6-line one."""
    return round(max(1.8, min(7.5, 1.4 + math.sqrt(max(loc, 1)) * 0.22)), 2)


def _short(module_id: str) -> str:
    """`app.handlers.pull_request.review` → `review`, truncated if still long."""
    tail = module_id.rsplit(".", 1)[-1] or module_id
    if len(tail) > MAX_LABEL_CHARS:
        return tail[: MAX_LABEL_CHARS - 1] + "…"
    return tail


def _ordered_nodes(nodes: list[dict]) -> list[dict]:
    """Group the ring by layer, and sort inside a layer by name.

    Both keys are total and data-independent, so the same graph always produces
    the same ring — which is what makes a diff of the committed SVG meaningful
    rather than noise.
    """
    return sorted(nodes, key=lambda n: (_layer_sort_key(n.get("layer", "other")), n.get("id", "")))


def _place(nodes: list[dict]) -> dict[str, dict]:
    """
    Assign every module an angle on the ring.

    A gap is left between layers so the groups are visible as groups. The gaps
    are taken out of the circle before the per-node share is computed, so the
    ring always closes exactly.
    """
    ordered = _ordered_nodes(nodes)
    if not ordered:
        return {}

    layers = []
    for n in ordered:
        layer = n.get("layer", "other")
        if not layers or layers[-1] != layer:
            layers.append(layer)
    gap_count = len(layers) if len(layers) > 1 else 0
    gap = math.radians(2.4)
    usable = (math.tau - gap * gap_count) if gap_count else math.tau
    step = usable / len(ordered)

    placed: dict[str, dict] = {}
    angle = -math.pi / 2  # start at 12 o'clock
    previous_layer = None
    for n in ordered:
        layer = n.get("layer", "other")
        if previous_layer is not None and layer != previous_layer:
            angle += gap
        placed[n["id"]] = {
            "node": n,
            "angle": angle,
            "x": CX + math.cos(angle) * RING_R,
            "y": CY + math.sin(angle) * RING_R,
        }
        angle += step
        previous_layer = layer
    return placed


# ── SVG pieces ───────────────────────────────────────────────────────────────


def _edge_paths(edges: list[dict], placed: dict[str, dict]) -> list[str]:
    """
    One bundled quadratic curve per import.

    Coloured by the *source* layer, so a dense outbound fan reads as one colour
    leaving one arc of the ring. Runtime imports (inside a function body) are
    drawn fainter and dashed: this codebase uses them deliberately to break
    cycles and keep optional dependencies optional, and showing them at the same
    weight as a top-level import overstates the coupling.
    """
    out: list[str] = []
    for e in edges:
        a = placed.get(e.get("source", ""))
        b = placed.get(e.get("target", ""))
        if not a or not b:
            # finalise() drops these, but a hand-edited payload might not.
            continue
        mx, my = (a["x"] + b["x"]) / 2, (a["y"] + b["y"]) / 2
        ctrl_x = CX + (mx - CX) * BUNDLE
        ctrl_y = CY + (my - CY) * BUNDLE
        runtime = e.get("kind") == "runtime"
        colour = _color(a["node"].get("layer", "other"))
        dash = ' stroke-dasharray="2 3"' if runtime else ""
        out.append(
            f'<path d="M{_r(a["x"])} {_r(a["y"])}Q{_r(ctrl_x)} {_r(ctrl_y)} '
            f'{_r(b["x"])} {_r(b["y"])}" stroke="{colour}" stroke-width="'
            f'{0.5 if runtime else 0.7}" opacity="{0.14 if runtime else 0.3}"{dash}/>'
        )
    return out


def _node_circles(placed: dict[str, dict]) -> list[str]:
    out: list[str] = []
    for entry in placed.values():
        n = entry["node"]
        out.append(
            f'<circle cx="{_r(entry["x"])}" cy="{_r(entry["y"])}" '
            f'r="{_node_radius(int(n.get("loc", 0) or 0))}" '
            f'fill="{_color(n.get("layer", "other"))}"/>'
        )
    return out


def _node_labels(placed: dict[str, dict]) -> list[str]:
    """
    Radial labels, rotated so every one reads left-to-right.

    Text on the left half of the circle is flipped 180° and right-aligned;
    without that, half the ring is upside down.
    """
    out: list[str] = []
    for entry in placed.values():
        n = entry["node"]
        angle = entry["angle"]
        deg = math.degrees(angle) % 360
        x = CX + math.cos(angle) * LABEL_R
        y = CY + math.sin(angle) * LABEL_R
        flip = 90 < deg < 270
        rotation = deg + 180 if flip else deg
        anchor = "end" if flip else "start"
        out.append(
            f'<text x="{_r(x)}" y="{_r(y)}" font-size="7.6" '
            f'fill="{_color(n.get("layer", "other"))}" text-anchor="{anchor}" '
            f'dominant-baseline="middle" opacity="0.92" '
            f'transform="rotate({_r(rotation)} {_r(x)} {_r(y)})">'
            f"{escape(_short(n.get('id', '')))}</text>"
        )
    return out


def _panel(payload: dict, nodes: list[dict]) -> list[str]:
    """The right-hand column: what the picture cannot say on its own."""
    stats = payload.get("stats") or {}
    cycles = stats.get("cycles") or []
    orphans = stats.get("orphans") or []
    hotspots = stats.get("hotspots") or []

    counts: dict[str, int] = {}
    for n in nodes:
        layer = n.get("layer", "other")
        counts[layer] = counts.get(layer, 0) + 1

    out: list[str] = []
    y = 64.0

    def line(text: str, *, size: float, fill: str, dy: float, weight: str = "400", x: float = 0.0):
        nonlocal y
        y += dy
        out.append(
            f'<text x="{_r(PANEL_X + x)}" y="{_r(y)}" font-size="{size}" '
            f'fill="{fill}" font-weight="{weight}">{escape(text)}</text>'
        )

    line("Module dependency map", size=19, fill=TEXT, dy=0, weight="650")
    line("Derived from the AST on every CI run — not hand-drawn.", size=11, fill=DIM, dy=20)

    # ── Numbers ──────────────────────────────────────────────────────────────
    y += 26
    out.append(
        f'<line x1="{_r(PANEL_X)}" y1="{_r(y)}" x2="{WIDTH - 40}" y2="{_r(y)}" '
        f'stroke="{BORDER}" stroke-width="1"/>'
    )

    def stat(label: str, value: str, colour: str = TEXT):
        nonlocal y
        y += 23
        out.append(
            f'<text x="{_r(PANEL_X)}" y="{_r(y)}" font-size="12" fill="{DIM}">'
            f"{escape(label)}</text>"
            f'<text x="{WIDTH - 40}" y="{_r(y)}" font-size="12" fill="{colour}" '
            f'text-anchor="end" font-weight="600">{escape(value)}</text>'
        )

    stat("Modules", f"{stats.get('modules', len(nodes)):,}")
    stat("Imports", f"{stats.get('edges', 0):,}")
    stat("Lines of code", f"{stats.get('total_loc', 0):,}")
    stat(
        "Import cycles",
        "none" if not cycles else f"{len(cycles)}",
        OK if not cycles else BAD,
    )
    stat(
        "Unreferenced modules",
        "none" if not orphans else f"{len(orphans)}",
        OK if not orphans else BAD,
    )

    # ── Legend ───────────────────────────────────────────────────────────────
    y += 30
    line("LAYERS", size=10, fill=DIM, dy=0, weight="600")
    for layer in sorted(counts, key=_layer_sort_key):
        y += 20
        out.append(
            f'<circle cx="{_r(PANEL_X + 5)}" cy="{_r(y - 4)}" r="5" fill="{_color(layer)}"/>'
            f'<text x="{_r(PANEL_X + 18)}" y="{_r(y)}" font-size="12" fill="{TEXT}">'
            f"{escape(layer)}</text>"
            f'<text x="{WIDTH - 40}" y="{_r(y)}" font-size="12" fill="{DIM}" '
            f'text-anchor="end">{counts[layer]}</text>'
        )

    # ── Hotspots ─────────────────────────────────────────────────────────────
    if hotspots:
        y += 30
        line("HOTSPOTS — size × how much depends on it", size=10, fill=DIM, dy=0, weight="600")
        for h in hotspots[:8]:
            y += 19
            out.append(
                f'<text x="{_r(PANEL_X)}" y="{_r(y)}" font-size="11.5" fill="{TEXT}">'
                f"{escape(_short(h.get('id', '')))}</text>"
                f'<text x="{WIDTH - 40}" y="{_r(y)}" font-size="11" fill="{DIM}" '
                f'text-anchor="end">{h.get("loc", 0)} lines · {h.get("fan_in", 0)} in</text>'
            )

    # ── Anything actually wrong ──────────────────────────────────────────────
    # Reported here rather than left to the reader to spot in the ring: a cycle
    # is a design defect and an unreferenced module is dead code or an
    # undeclared entrypoint. Both block CI, so if they appear in this picture
    # something is already failing.
    for title, items, colour in (
        ("IMPORT CYCLES", [" → ".join(c) for c in cycles], BAD),
        ("NOTHING IMPORTS THESE", list(orphans), BAD),
    ):
        if not items:
            continue
        y += 28
        line(title, size=10, fill=colour, dy=0, weight="600")
        for item in items[:5]:
            y += 17
            out.append(
                f'<text x="{_r(PANEL_X)}" y="{_r(y)}" font-size="11" fill="{colour}">'
                f"{escape(item[:56])}</text>"
            )

    return out


def render_svg(payload: dict) -> str:
    """
    Render `CodeGraph.to_dict()` (optionally with `stats.orphans`) as an SVG.

    The result is a complete standalone document: no script, no external font,
    no network reference. That is a requirement, not a style choice — GitHub
    renders a repository SVG inside an `<img>`, which executes nothing and
    fetches nothing, so anything that needed either would simply not appear.
    """
    nodes = list(payload.get("nodes") or [])
    edges = list(payload.get("edges") or [])
    stats = payload.get("stats") or {}
    placed = _place(nodes)

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'width="{WIDTH}" height="{HEIGHT}" role="img" '
        f'aria-label="Module dependency map: {stats.get("modules", len(nodes))} modules, '
        f'{stats.get("edges", len(edges))} imports">',
        "<title>GitHub Autopilot — module dependency map</title>",
        (
            "<desc>Every Python module in this repository, arranged on a ring and "
            "grouped by layer. Curves are imports; dashed curves are imports made "
            "inside a function body. Generated from the AST — see "
            "app/intelligence/codegraph.py.</desc>"
        ),
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="{BG}"/>',
        f'<rect x="{_r(PANEL_X - 26)}" y="24" width="{_r(WIDTH - PANEL_X + 2)}" '
        f'height="{HEIGHT - 48}" rx="14" fill="{PANEL}" stroke="{BORDER}"/>',
        # One font stack for the whole document. Generic families only: a web
        # font would be an external fetch, which an <img>-embedded SVG cannot make.
        '<g font-family="ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, '
        'Helvetica, Arial, sans-serif">',
        '<g fill="none" stroke-linecap="round">',
        *_edge_paths(edges, placed),
        "</g>",
        *_node_circles(placed),
        *_node_labels(placed),
        *_panel(payload, nodes),
        f'<text x="{_r(CX)}" y="{HEIGHT - 26}" font-size="10.5" fill="{DIM}" '
        f'text-anchor="middle">Solid curve: module-level import · '
        f"Dashed: import inside a function · Dot size: lines of code</text>",
        "</g>",
        "</svg>",
    ]
    return "\n".join(parts) + "\n"
