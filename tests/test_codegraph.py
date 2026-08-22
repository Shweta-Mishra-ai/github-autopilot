"""
tests/test_codegraph.py

Tests for the AST dependency-graph extractor behind the /graph view.

The extractor never imports the code it analyses, so these build real files in
tmp_path and assert on the resulting graph. The cases that matter most are
relative-import resolution (most imports inside a package are relative, and
getting the base wrong mis-attributes all of them) and the runtime-vs-top-level
distinction (this codebase breaks cycles with function-local imports, so
counting them as ordinary edges would report cycles that do not exist).
"""

from __future__ import annotations

import json

import pytest

from app.intelligence.codegraph import (
    CodeGraph,
    ModuleNode,
    _layer_for,
    _module_id,
    _resolve_relative,
    build_graph,
    iter_python_files,
    main,
)


def _write(root, rel_path: str, source: str):
    p = root / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(source, encoding="utf-8")
    return p


@pytest.fixture
def pkg(tmp_path):
    """A small package with the import shapes this codebase actually uses."""
    _write(tmp_path, "pkg/__init__.py", "from .core import helper\n")
    _write(tmp_path, "pkg/core.py", "import os\n\ndef helper():\n    return os.sep\n")
    _write(
        tmp_path,
        "pkg/service.py",
        "from .core import helper\nimport requests\n\n"
        "def run():\n    from .heavy import work\n    return work(helper())\n",
    )
    _write(tmp_path, "pkg/heavy.py", "def work(x):\n    return x\n")
    _write(tmp_path, "pkg/unused.py", "def nobody_calls_me():\n    return 1\n")
    return tmp_path


class TestModuleIdentity:
    def test_module_file_becomes_dotted_path(self, tmp_path):
        f = _write(tmp_path, "app/core/config.py", "")
        assert _module_id(f, tmp_path) == "app.core.config"

    def test_package_init_becomes_the_package(self, tmp_path):
        f = _write(tmp_path, "app/handlers/__init__.py", "")
        assert _module_id(f, tmp_path) == "app.handlers"

    def test_root_level_file(self, tmp_path):
        f = _write(tmp_path, "server.py", "")
        assert _module_id(f, tmp_path) == "server"


class TestRelativeImportResolution:
    def test_sibling_import_from_a_module(self):
        """`from .classify import x` inside app.handlers.pr.review."""
        assert (
            _resolve_relative("app.handlers.pr.review", False, 1, "classify")
            == "app.handlers.pr.classify"
        )

    def test_sibling_import_from_a_package_init(self):
        """A package is its own base at level 1 — getting this backwards
        mis-attributes every relative import in every __init__.py."""
        assert _resolve_relative("app.handlers.pr", True, 1, "review") == "app.handlers.pr.review"

    def test_parent_package_import(self):
        """`from ..shared import x` in app/handlers/pr/review.py resolves to
        app.handlers.shared: level 1 is the containing package, level 2 its
        parent. Verified against CPython's own import machinery."""
        assert (
            _resolve_relative("app.handlers.pr.review", False, 2, "shared")
            == "app.handlers.shared"
        )

    def test_grandparent_package_import(self):
        assert (
            _resolve_relative("app.handlers.pr.review", False, 3, "shared") == "app.shared"
        )

    def test_bare_relative_import_without_module(self):
        assert _resolve_relative("app.handlers.pr.review", False, 1, None) == "app.handlers.pr"


class TestLayering:
    @pytest.mark.parametrize(
        "module_id,layer",
        [
            ("app.handlers.push", "handlers"),
            ("app.ai.router", "ai"),
            ("app.core.config", "core"),
            ("app.security.scanner", "security"),
            ("server", "other"),
        ],
    )
    def test_layer_inference(self, module_id, layer):
        assert _layer_for(module_id) == layer

    def test_specific_prefix_wins_over_parent(self):
        """LAYER_RULES is first-match, so ordering must put app.handlers before
        any broader app.* rule."""
        assert _layer_for("app.handlers.comments.service") == "handlers"

    def test_prefix_match_requires_a_boundary(self):
        """app.airplane must not match the app.ai rule."""
        assert _layer_for("app.airplane") == "other"


class TestBuildGraph:
    def test_finds_every_module(self, pkg):
        g = build_graph("pkg", root=pkg)
        assert set(g.nodes) == {
            "pkg",
            "pkg.core",
            "pkg.service",
            "pkg.heavy",
            "pkg.unused",
        }

    def test_internal_edge_is_recorded(self, pkg):
        g = build_graph("pkg", root=pkg)
        assert any(e.source == "pkg.service" and e.target == "pkg.core" for e in g.edges)

    def test_external_import_does_not_become_a_node(self, pkg):
        """A graph where `logging` is the most-connected node says nothing
        about your own design."""
        g = build_graph("pkg", root=pkg)
        assert "requests" not in g.nodes
        assert "os" not in g.nodes

    def test_external_import_is_recorded_on_the_importer(self, pkg):
        g = build_graph("pkg", root=pkg)
        assert "requests" in g.nodes["pkg.service"].external_deps
        assert "os" in g.nodes["pkg.core"].external_deps

    def test_function_local_import_is_marked_runtime(self, pkg):
        g = build_graph("pkg", root=pkg)
        edge = next(e for e in g.edges if e.target == "pkg.heavy")
        assert edge.kind == "runtime"

    def test_module_level_import_is_marked_import(self, pkg):
        g = build_graph("pkg", root=pkg)
        edge = next(
            e for e in g.edges if e.source == "pkg.service" and e.target == "pkg.core"
        )
        assert edge.kind == "import"

    def test_degrees_are_computed(self, pkg):
        g = build_graph("pkg", root=pkg)
        assert g.nodes["pkg.core"].fan_in >= 1
        assert g.nodes["pkg.service"].fan_out >= 1
        assert g.nodes["pkg.unused"].fan_in == 0

    def test_counts_functions_and_classes(self, tmp_path):
        _write(
            tmp_path,
            "m/__init__.py",
            "class A:\n    def method(self):\n        pass\n\ndef top():\n    pass\n",
        )
        g = build_graph("m", root=tmp_path)
        n = g.nodes["m"]
        assert n.classes == 1
        assert n.functions == 1, "methods must not be counted as module functions"

    def test_no_self_edges(self, tmp_path):
        _write(tmp_path, "m/__init__.py", "import m\n")
        g = build_graph("m", root=tmp_path)
        assert all(e.source != e.target for e in g.edges)

    def test_duplicate_imports_collapse_to_one_edge(self, tmp_path):
        _write(tmp_path, "m/__init__.py", "")
        _write(tmp_path, "m/a.py", "")
        _write(tmp_path, "m/b.py", "from .a import x\nfrom .a import y\nimport m.a\n")
        g = build_graph("m", root=tmp_path)
        top = [e for e in g.edges if e.source == "m.b" and e.target == "m.a"]
        assert len(top) == 1

    def test_syntax_error_skips_one_file_not_the_run(self, tmp_path):
        _write(tmp_path, "m/__init__.py", "")
        _write(tmp_path, "m/good.py", "x = 1\n")
        _write(tmp_path, "m/broken.py", "def (((\n")
        g = build_graph("m", root=tmp_path)
        assert "m.good" in g.nodes
        assert "m.broken" not in g.nodes

    def test_missing_target_is_tolerated(self, tmp_path):
        assert build_graph("does_not_exist", root=tmp_path).nodes == {}

    def test_vendored_directories_are_skipped(self, tmp_path):
        _write(tmp_path, "m/__init__.py", "")
        _write(tmp_path, "m/node_modules/pkg/x.py", "")
        _write(tmp_path, "m/__pycache__/y.py", "")
        g = build_graph("m", root=tmp_path)
        assert set(g.nodes) == {"m"}

    def test_single_file_target(self, tmp_path):
        """server.py and worker.py are root-level entrypoints, not packages.
        Omitting them makes every handler they import look like an orphan."""
        _write(tmp_path, "m/__init__.py", "")
        _write(tmp_path, "m/handler.py", "")
        _write(tmp_path, "server.py", "from m.handler import go\n")
        g = build_graph("m", "server.py", root=tmp_path)
        assert "server" in g.nodes
        assert g.nodes["m.handler"].fan_in == 1


class TestIterPythonFiles:
    def test_non_python_file_target_yields_nothing(self, tmp_path):
        p = _write(tmp_path, "notes.md", "hello")
        assert iter_python_files(p) == []

    def test_directory_recursion(self, tmp_path):
        _write(tmp_path, "a/b/c/deep.py", "")
        assert len(iter_python_files(tmp_path)) == 1


class TestOrphans:
    def test_unimported_module_is_an_orphan(self, pkg):
        g = build_graph("pkg", root=pkg)
        assert "pkg.unused" in g.orphans()

    def test_entrypoints_are_not_orphans(self, pkg):
        g = build_graph("pkg", root=pkg)
        assert "pkg.unused" not in g.orphans(entrypoints=("pkg.unused",))

    def test_packages_are_never_orphans(self, tmp_path):
        """A package __init__ exists to be imported from outside."""
        _write(tmp_path, "m/__init__.py", "")
        g = build_graph("m", root=tmp_path)
        assert "m" not in g.orphans()

    def test_runtime_only_import_still_counts_as_used(self, pkg):
        """pkg.heavy is imported only inside a function. It is used."""
        g = build_graph("pkg", root=pkg)
        assert "pkg.heavy" not in g.orphans()


class TestCycles:
    def test_no_cycle_in_an_acyclic_graph(self, pkg):
        assert build_graph("pkg", root=pkg).cycles() == []

    def test_detects_a_two_module_cycle(self, tmp_path):
        _write(tmp_path, "m/__init__.py", "")
        _write(tmp_path, "m/a.py", "from .b import x\n")
        _write(tmp_path, "m/b.py", "from .a import y\n")
        cycles = build_graph("m", root=tmp_path).cycles()
        assert cycles == [["m.a", "m.b"]]

    def test_detects_a_three_module_cycle(self, tmp_path):
        _write(tmp_path, "m/__init__.py", "")
        _write(tmp_path, "m/a.py", "from .b import x\n")
        _write(tmp_path, "m/b.py", "from .c import x\n")
        _write(tmp_path, "m/c.py", "from .a import x\n")
        assert build_graph("m", root=tmp_path).cycles() == [["m.a", "m.b", "m.c"]]

    def test_runtime_import_does_not_create_a_cycle(self, tmp_path):
        """Deferring an import into a function is THE standard fix for a cycle.
        Counting it would report every cycle that has already been fixed."""
        _write(tmp_path, "m/__init__.py", "")
        _write(tmp_path, "m/a.py", "from .b import x\n")
        _write(tmp_path, "m/b.py", "def f():\n    from .a import y\n    return y\n")
        assert build_graph("m", root=tmp_path).cycles() == []

    def test_deep_chain_does_not_hit_the_recursion_limit(self, tmp_path):
        _write(tmp_path, "m/__init__.py", "")
        depth = 400
        for i in range(depth):
            nxt = f"from .n{i + 1} import x\n" if i < depth - 1 else ""
            _write(tmp_path, f"m/n{i}.py", nxt)
        assert build_graph("m", root=tmp_path).cycles() == []


class TestHotspots:
    def test_ranks_large_and_depended_on_modules_first(self):
        g = CodeGraph()
        g.add_module(ModuleNode(id="big_lonely", path="a.py", layer="core", loc=1000))
        g.add_module(ModuleNode(id="small_hub", path="b.py", layer="core", loc=50))
        g.add_module(ModuleNode(id="big_hub", path="c.py", layer="core", loc=500))
        for i in range(20):
            g.add_module(ModuleNode(id=f"u{i}", path=f"u{i}.py", layer="core", loc=1))
            g.add_edge(f"u{i}", "big_hub")
            g.add_edge(f"u{i}", "small_hub")
        g.finalise()
        assert g.hotspots(limit=1)[0]["id"] == "big_hub"

    def test_limit_is_respected(self, pkg):
        assert len(build_graph("pkg", root=pkg).hotspots(limit=2)) == 2


class TestSerialisation:
    def test_to_dict_is_json_serialisable(self, pkg):
        payload = build_graph("pkg", root=pkg).to_dict()
        assert json.loads(json.dumps(payload))

    def test_to_dict_carries_stats(self, pkg):
        stats = build_graph("pkg", root=pkg).to_dict()["stats"]
        assert stats["modules"] == 5
        assert "cycles" in stats
        assert "hotspots" in stats
        assert stats["total_loc"] > 0

    def test_nodes_are_sorted_for_a_stable_diff(self, pkg):
        """The JSON is committed by CI; unstable ordering would produce a diff
        on every run regardless of whether the code changed."""
        ids = [n["id"] for n in build_graph("pkg", root=pkg).to_dict()["nodes"]]
        assert ids == sorted(ids)

    def test_mermaid_output_is_a_graph(self, pkg):
        out = build_graph("pkg", root=pkg).to_mermaid()
        assert out.startswith("graph LR")


class TestCli:
    def test_writes_json_to_out_path(self, pkg, capsys):
        out = pkg / "out" / "graph.json"
        assert main(["pkg", "--root", str(pkg), "--out", str(out)]) == 0
        payload = json.loads(out.read_text())
        assert payload["stats"]["modules"] == 5
        assert "orphans" in payload["stats"]

    def test_prints_json_to_stdout_by_default(self, pkg, capsys):
        assert main(["pkg", "--root", str(pkg)]) == 0
        assert json.loads(capsys.readouterr().out)["nodes"]

    def test_mermaid_flag(self, pkg, capsys):
        assert main(["pkg", "--root", str(pkg), "--mermaid"]) == 0
        assert "graph LR" in capsys.readouterr().out

    def test_entrypoint_flag_suppresses_an_orphan(self, pkg, capsys):
        main(["pkg", "--root", str(pkg), "--entrypoint", "pkg.unused"])
        payload = json.loads(capsys.readouterr().out)
        assert "pkg.unused" not in payload["stats"]["orphans"]


class TestAgainstThisRepository:
    """Smoke tests over the real tree — the extractor must survive it."""

    def test_builds_without_error(self):
        g = build_graph("app", "server.py", "worker.py", root=".")
        assert len(g.nodes) > 50
        assert len(g.edges) > 50

    def test_known_hub_has_high_fan_in(self):
        g = build_graph("app", "server.py", "worker.py", root=".")
        assert g.nodes["app.github.client"].fan_in > 5

    def test_every_edge_endpoint_exists(self):
        g = build_graph("app", "server.py", "worker.py", root=".")
        for e in g.edges:
            assert e.source in g.nodes
            assert e.target in g.nodes
