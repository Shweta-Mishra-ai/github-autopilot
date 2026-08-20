"""
app/intelligence/codegraph.py
Build a dependency graph of a Python codebase by reading its AST.

Why this exists
───────────────
The hand-drawn diagrams in docs/ are accurate the day they are written and
quietly wrong three refactors later, with nothing to signal the drift. This
derives the same picture from the code itself, so it cannot go stale.

It answers questions a folder tree cannot:

  - What actually depends on this module? (fan-in)
  - Is anything importing this at all, or is it dead? (orphans)
  - Are there import cycles? (they make modules impossible to test alone)
  - Where is the weight concentrated? (LOC + fan-in together)

Pure stdlib — `ast` only. No network, no third-party parser, and it never
imports the code it analyses, so running it on an untrusted repository executes
nothing from that repository.

Usage
─────
    from app.intelligence.codegraph import build_graph
    graph = build_graph("app")
    graph.to_dict()          # JSON-serialisable, what the /graph view renders

    python -m app.intelligence.codegraph app --out docs/diagrams/codegraph.json
"""

from __future__ import annotations

import ast
import json
import logging
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

log = logging.getLogger(__name__)

# Directories never worth graphing — vendored code, caches, build output.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        "node_modules",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "build",
        "dist",
        "site-packages",
        ".eggs",
    }
)

# Layer inference. First match wins, so order matters: the more specific
# prefixes must precede their parents.
LAYER_RULES: tuple[tuple[str, str], ...] = (
    ("app.handlers", "handlers"),
    ("app.ai", "ai"),
    ("app.core", "core"),
    ("app.github", "github"),
    ("app.security", "security"),
    ("app.intelligence", "intelligence"),
    ("app.mcp", "mcp"),
    ("tests", "tests"),
    ("evals", "evals"),
)
DEFAULT_LAYER = "other"


@dataclass
class ModuleNode:
    """One Python module. `id` is its dotted import path."""

    id: str
    path: str
    layer: str
    loc: int = 0
    functions: int = 0
    classes: int = 0
    is_package: bool = False
    # Populated by CodeGraph.finalise()
    fan_in: int = 0
    fan_out: int = 0
    external_deps: list[str] = field(default_factory=list)


@dataclass
class Edge:
    source: str
    target: str
    # "import" — a module-level import. "runtime" — an import inside a
    # function body, which this codebase uses deliberately to break cycles and
    # to keep optional dependencies optional. Worth distinguishing: a runtime
    # edge is a much weaker coupling than a top-level one.
    kind: str = "import"


class CodeGraph:
    def __init__(self) -> None:
        self.nodes: dict[str, ModuleNode] = {}
        self.edges: list[Edge] = []

    # ── Building ────────────────────────────────────────────────────────────

    def add_module(self, node: ModuleNode) -> None:
        self.nodes[node.id] = node

    def add_edge(self, source: str, target: str, kind: str = "import") -> None:
        self.edges.append(Edge(source=source, target=target, kind=kind))

    def finalise(self) -> "CodeGraph":
        """
        Drop edges pointing outside the graph, deduplicate, and compute degrees.

        External imports (requests, flask, ...) are recorded on the importing
        node rather than becoming nodes of their own: a hairball where `logging`
        is the most connected node tells you nothing about your own design.
        """
        internal: list[Edge] = []
        seen: set[tuple[str, str, str]] = set()

        for e in self.edges:
            if e.source not in self.nodes:
                continue
            if e.target in self.nodes:
                key = (e.source, e.target, e.kind)
                if key in seen or e.source == e.target:
                    continue
                seen.add(key)
                internal.append(e)
            else:
                deps = self.nodes[e.source].external_deps
                root = e.target.split(".")[0]
                if root and root not in deps:
                    deps.append(root)

        self.edges = internal

        for n in self.nodes.values():
            n.fan_in = 0
            n.fan_out = 0
            n.external_deps.sort()
        for e in self.edges:
            self.nodes[e.source].fan_out += 1
            self.nodes[e.target].fan_in += 1

        return self

    # ── Analysis ────────────────────────────────────────────────────────────

    def orphans(self, entrypoints: tuple[str, ...] = ()) -> list[str]:
        """
        Modules nothing imports — candidate dead code.

        `entrypoints` names modules that are meant to have no importers (a CLI,
        a WSGI target, a package __init__ re-exporting for outside callers).
        Without that argument every legitimate entrypoint reads as dead.
        """
        roots = set(entrypoints)
        return sorted(
            n.id
            for n in self.nodes.values()
            if n.fan_in == 0 and n.id not in roots and not n.is_package
        )

    def cycles(self) -> list[list[str]]:
        """
        Import cycles, as lists of module ids.

        Only top-level ("import") edges count: a runtime import inside a
        function is the standard way to break a cycle, so counting it would
        report every cycle this codebase has already fixed.

        Iterative Tarjan — recursion would blow the stack on a large repo.
        """
        adj: dict[str, list[str]] = {n: [] for n in self.nodes}
        for e in self.edges:
            if e.kind == "import":
                adj[e.source].append(e.target)

        index: dict[str, int] = {}
        low: dict[str, int] = {}
        on_stack: set[str] = set()
        stack: list[str] = []
        counter = 0
        found: list[list[str]] = []

        for root in adj:
            if root in index:
                continue
            work: list[tuple[str, int]] = [(root, 0)]
            while work:
                node, child_i = work[-1]
                if child_i == 0:
                    index[node] = low[node] = counter
                    counter += 1
                    stack.append(node)
                    on_stack.add(node)

                recursed = False
                children = adj[node]
                while child_i < len(children):
                    child = children[child_i]
                    child_i += 1
                    if child not in index:
                        work[-1] = (node, child_i)
                        work.append((child, 0))
                        recursed = True
                        break
                    if child in on_stack:
                        low[node] = min(low[node], index[child])
                else:
                    work[-1] = (node, child_i)

                if recursed:
                    continue

                work.pop()
                if low[node] == index[node]:
                    component = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        component.append(w)
                        if w == node:
                            break
                    if len(component) > 1:
                        found.append(sorted(component))
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])

        return sorted(found)

    def hotspots(self, limit: int = 10) -> list[dict]:
        """
        Modules where size and coupling coincide — the ones most expensive to
        change. Ranked by loc * (fan_in + 1): a large module nothing depends on
        is merely long, while a large module everything depends on is risk.
        """
        ranked = sorted(
            self.nodes.values(),
            key=lambda n: n.loc * (n.fan_in + 1),
            reverse=True,
        )
        return [
            {"id": n.id, "loc": n.loc, "fan_in": n.fan_in, "fan_out": n.fan_out}
            for n in ranked[:limit]
        ]

    # ── Serialisation ───────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "nodes": [asdict(n) for n in sorted(self.nodes.values(), key=lambda n: n.id)],
            "edges": [asdict(e) for e in sorted(self.edges, key=lambda e: (e.source, e.target))],
            "stats": {
                "modules": len(self.nodes),
                "edges": len(self.edges),
                "total_loc": sum(n.loc for n in self.nodes.values()),
                "layers": sorted({n.layer for n in self.nodes.values()}),
                "cycles": self.cycles(),
                "hotspots": self.hotspots(),
            },
        }

    def to_mermaid(self, max_nodes: int = 40) -> str:
        """
        Mermaid rendering, for places that cannot run the interactive view
        (a README, a PR comment). Collapses to package level — a per-module
        mermaid graph of a real codebase is unreadable.
        """
        pkg_edges: set[tuple[str, str]] = set()
        for e in self.edges:
            s = self.nodes[e.source].layer
            t = self.nodes[e.target].layer
            if s != t:
                pkg_edges.add((s, t))

        lines = ["graph LR"]
        for layer in sorted({n.layer for n in self.nodes.values()}):
            count = sum(1 for n in self.nodes.values() if n.layer == layer)
            lines.append(f'    {_mermaid_id(layer)}["{layer}<br/>{count} modules"]')
        for s, t in sorted(pkg_edges)[:max_nodes]:
            lines.append(f"    {_mermaid_id(s)} --> {_mermaid_id(t)}")
        return "\n".join(lines)


def _mermaid_id(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)


# ── Extraction ──────────────────────────────────────────────────────────────


def _module_id(file_path: Path, root: Path) -> str:
    """Dotted import path for a file, relative to the project root."""
    rel = file_path.relative_to(root)
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]  # strip .py
    return ".".join(parts)


def _layer_for(module_id: str) -> str:
    for prefix, layer in LAYER_RULES:
        if module_id == prefix or module_id.startswith(prefix + "."):
            return layer
    return DEFAULT_LAYER


def _resolve_relative(module_id: str, is_package: bool, level: int, target: str | None) -> str:
    """
    Resolve `from ..x import y` to an absolute dotted path.

    A package (__init__.py) is its own base at level 1; a module's base is its
    containing package. Getting this backwards silently mis-attributes every
    relative import inside a package, which is most of them in this codebase.
    """
    parts = module_id.split(".")
    base = parts if is_package else parts[:-1]
    # level 1 == current package; each extra level goes one further up.
    trimmed = base[: len(base) - (level - 1)] if level > 1 else base
    if target:
        trimmed = trimmed + target.split(".")
    return ".".join([p for p in trimmed if p])


class _ImportVisitor(ast.NodeVisitor):
    """
    Collect imports, distinguishing module-level from function-local.

    Tracked with a depth counter rather than by visiting FunctionDef children
    separately, so an import nested several scopes deep is still classified as
    runtime.
    """

    def __init__(self, module_id: str, is_package: bool) -> None:
        self.module_id = module_id
        self.is_package = is_package
        self.imports: list[tuple[str, str]] = []  # (target, kind)
        self._depth = 0
        self.functions = 0
        self.classes = 0

    @property
    def _kind(self) -> str:
        return "runtime" if self._depth > 0 else "import"

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append((alias.name, self._kind))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            resolved = _resolve_relative(self.module_id, self.is_package, node.level, node.module)
        else:
            resolved = node.module or ""
        if not resolved:
            return
        self.imports.append((resolved, self._kind))
        # `from app.core import config` imports a submodule, not an attribute —
        # record both so whichever exists in the graph resolves.
        for alias in node.names:
            self.imports.append((f"{resolved}.{alias.name}", self._kind))

    def _visit_scope(self, node) -> None:
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if self._depth == 0:
            self.functions += 1
        self._visit_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if self._depth == 0:
            self.functions += 1
        self._visit_scope(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if self._depth == 0:
            self.classes += 1
        # Methods are counted via the depth counter, not as module functions.
        self._visit_scope(node)


def iter_python_files(root: Path) -> list[Path]:
    """
    Every .py file under root, skipping vendored and generated trees.

    A path to a single .py file is also accepted, so entrypoints that live at
    the repository root (server.py, worker.py) can be included. Leaving them
    out is not cosmetic: they import most of the handler modules, so without
    them every handler looks like an orphan.
    """
    if root.is_file():
        return [root] if root.suffix == ".py" else []

    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in sorted(filenames):
            if name.endswith(".py"):
                found.append(Path(dirpath) / name)
    return sorted(found)


def build_graph(
    *targets: str,
    root: str | os.PathLike | None = None,
) -> CodeGraph:
    """
    Build the dependency graph for one or more directories.

    `targets` are paths relative to `root` (default: the current directory).
    A file that fails to parse is skipped with a warning rather than aborting
    the run — a syntax error in one module should not cost you the graph.
    """
    root_path = Path(root or ".").resolve()
    graph = CodeGraph()
    pending: list[tuple[str, list[tuple[str, str]]]] = []

    for target in targets or ("app",):
        target_path = (root_path / target).resolve()
        if not target_path.exists():
            log.warning(f"codegraph.target_missing path={target_path}")
            continue

        for file_path in iter_python_files(target_path):
            try:
                source = file_path.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(file_path))
            except (SyntaxError, UnicodeDecodeError, OSError) as exc:
                log.warning(f"codegraph.parse_failed file={file_path}: {exc}")
                continue

            module_id = _module_id(file_path, root_path)
            is_package = file_path.name == "__init__.py"

            visitor = _ImportVisitor(module_id, is_package)
            visitor.visit(tree)

            graph.add_module(
                ModuleNode(
                    id=module_id,
                    path=str(file_path.relative_to(root_path)),
                    layer=_layer_for(module_id),
                    loc=source.count("\n") + 1,
                    functions=visitor.functions,
                    classes=visitor.classes,
                    is_package=is_package,
                )
            )
            pending.append((module_id, visitor.imports))

    # Edges are added after every node exists, so finalise() can tell an
    # internal target from an external one.
    for module_id, imports in pending:
        for target_mod, kind in imports:
            graph.add_edge(module_id, target_mod, kind)

    return graph.finalise()


# ── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m app.intelligence.codegraph",
        description="Build a dependency graph of a Python codebase from its AST.",
    )
    parser.add_argument("targets", nargs="*", default=["app"], help="directories to scan")
    parser.add_argument("--root", default=".", help="project root (default: cwd)")
    parser.add_argument("--out", help="write JSON here (default: stdout)")
    parser.add_argument("--mermaid", action="store_true", help="print a mermaid diagram instead")
    parser.add_argument(
        "--entrypoint",
        action="append",
        default=[],
        help="module with no expected importers; repeatable",
    )
    args = parser.parse_args(argv)

    graph = build_graph(*args.targets, root=args.root)

    if args.mermaid:
        print(graph.to_mermaid())
        return 0

    payload = graph.to_dict()
    payload["stats"]["orphans"] = graph.orphans(tuple(args.entrypoint))
    text = json.dumps(payload, indent=2, sort_keys=False)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
        stats = payload["stats"]
        print(
            f"codegraph: {stats['modules']} modules, {stats['edges']} edges, "
            f"{len(stats['cycles'])} cycles, {len(stats['orphans'])} orphans "
            f"-> {out_path}"
        )
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
