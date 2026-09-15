"""
tests/test_mcp_codebase_map.py

The codebase_map MCP tool — the one handler that reads the filesystem, and the
largest uncovered block in app/mcp/handlers.py.

It advertises itself as "read-only and local", and it read a `root` argument
that does not appear in its inputSchema. No client could discover it; any
client could use it:

    {"root": "/tmp", "targets": ["outside_project"]}

returned a full structural map of code outside the deployment — module names,
file paths, line counts, external dependencies. A tool that promises a map of
"the codebase" should not also be a filesystem enumeration primitive, whatever
key the caller holds.
"""

from __future__ import annotations

import pathlib

import pytest

from app.mcp.handlers import _handle_codebase_map, _project_root


@pytest.fixture
def outside_tree(tmp_path):
    """A Python file that is definitely not part of this deployment."""
    d = tmp_path / "not_our_code"
    d.mkdir()
    (d / "deploy_secrets.py").write_text("def key():\n    return 1\n")
    return d


def _is_a_map(text: str) -> bool:
    return text.startswith("## Codebase map")


class TestItStaysInsideTheDeployment:
    def test_the_undeclared_root_argument_no_longer_redirects_the_scan(self, outside_tree):
        out = _handle_codebase_map(
            {"root": str(outside_tree.parent), "targets": [outside_tree.name]}
        )
        assert "deploy_secrets" not in out, "root still redirects the scan"

    def test_an_absolute_target_is_refused(self, outside_tree):
        out = _handle_codebase_map({"targets": [str(outside_tree)]})
        assert "outside this deployment" in out
        assert "deploy_secrets" not in out

    def test_a_traversing_target_is_refused(self):
        out = _handle_codebase_map({"targets": ["../../../etc"]})
        assert "outside this deployment" in out

    def test_the_refusal_does_not_echo_the_resolved_path(self, outside_tree):
        """The old error came from deep in codegraph and read
        "'/tmp/.../deploy_secrets.py' is not in the subpath of '/home/...'",
        which hands back both the absolute path it reached and the absolute
        path of the deployment — the disclosure this is meant to prevent."""
        out = _handle_codebase_map({"targets": [str(outside_tree)]})
        assert str(_project_root()) not in out
        assert "deploy_secrets.py" not in out

    def test_a_target_that_merely_looks_like_traversal_still_works(self):
        """A path containing '..' that resolves back inside is fine — the rule
        is about where it lands, not how it is spelled."""
        out = _handle_codebase_map({"targets": ["app/../app"]})
        assert _is_a_map(out)

    def test_the_project_root_is_taken_from_the_package_not_the_cwd(self, monkeypatch, tmp_path):
        """The working directory is whatever the process was started in; this
        is a security boundary, so it cannot depend on that."""
        monkeypatch.chdir(tmp_path)
        assert (_project_root() / "app" / "mcp" / "handlers.py").exists()
        assert _is_a_map(_handle_codebase_map({}))


class TestTheMapItself:
    def test_the_default_call_maps_this_repository(self):
        out = _handle_codebase_map({})
        assert _is_a_map(out)
        assert "Most depended-on" in out
        assert "Import cycles" in out

    def test_a_focused_module_reports_both_directions(self):
        out = _handle_codebase_map({"module": "app.github.client"})
        assert "`app.github.client`" in out
        assert "Imported by" in out
        assert "Imports" in out

    def test_a_focused_module_names_its_layer_and_size(self):
        out = _handle_codebase_map({"module": "app.github.client"})
        assert "Layer:" in out and "Lines:" in out

    def test_an_unknown_module_suggests_near_matches(self):
        """A typo is the common case, and "not found" with no help means the
        caller has to fetch the whole map to find the spelling."""
        out = _handle_codebase_map({"module": "client"})
        assert "No module" in out
        assert "Did you mean" in out
        assert "app.github.client" in out

    def test_an_unknown_module_with_no_near_match_says_so_plainly(self):
        out = _handle_codebase_map({"module": "zzz_nothing_like_this"})
        assert "No module" in out
        assert "Did you mean" not in out

    def test_a_target_with_no_python_in_it_says_so(self):
        assert (_project_root() / "docs").exists(), "this test needs a real non-Python directory"
        assert "No Python modules found" in _handle_codebase_map({"targets": ["docs"]})

    def test_a_missing_target_does_not_raise(self):
        """Every _handle_* is documented never to raise — MCP turns an
        exception into a transport error with no useful message."""
        out = _handle_codebase_map({"targets": ["no_such_directory_here"]})
        assert isinstance(out, str) and out

    def test_entrypoints_suppress_the_dead_code_section(self):
        """Modules nothing imports are reported as dead-code candidates, and
        the real entrypoints always look like that."""
        with_defaults = _handle_codebase_map({})
        assert "Imported by nothing" not in with_defaults

    def test_bad_entrypoints_surface_the_orphans(self):
        out = _handle_codebase_map({"entrypoints": ["nothing_is_called_this"]})
        assert "Imported by nothing" in out


class TestItNeverRaises:
    @pytest.mark.parametrize(
        "args",
        [
            {},
            {"module": ""},
            {"module": None},
            {"targets": []},
            {"targets": None},
            {"targets": ["app"], "module": "app"},
            {"entrypoints": []},
            {"root": None},
            {"root": ""},
        ],
    )
    def test_odd_arguments_return_a_string(self, args):
        out = _handle_codebase_map(args)
        assert isinstance(out, str) and out

    def test_a_build_failure_is_reported_not_raised(self, monkeypatch):
        import app.intelligence.codegraph as cg

        def boom(*a, **k):
            raise RuntimeError("AST exploded")

        monkeypatch.setattr(cg, "build_graph", boom)
        out = _handle_codebase_map({})
        assert out.startswith("Error:")
        assert "AST exploded" in out

    def test_a_very_long_target_is_truncated_in_the_refusal(self):
        out = _handle_codebase_map({"targets": ["/" + "a" * 5000]})
        assert len(out) < 300


class TestRegistered:
    def test_the_tool_is_wired_to_this_handler(self):
        from app.mcp.handlers import TOOL_HANDLERS

        assert TOOL_HANDLERS["codebase_map"] is _handle_codebase_map

    def test_root_is_not_advertised_in_the_schema(self):
        """It was read but never declared. Now it is neither."""
        from app.mcp.tools import MCP_TOOLS

        schema = next(t for t in MCP_TOOLS if t["name"] == "codebase_map")["inputSchema"]
        assert "root" not in schema["properties"]

    def test_every_advertised_property_is_one_the_handler_reads(self):
        """The inverse of the bug: a schema that promises something the handler
        ignores is the same class of lie, just the harmless direction."""
        import inspect

        from app.mcp.tools import MCP_TOOLS

        schema = next(t for t in MCP_TOOLS if t["name"] == "codebase_map")["inputSchema"]
        source = inspect.getsource(_handle_codebase_map)
        for name in schema["properties"]:
            assert f'"{name}"' in source, f"schema advertises {name}, handler never reads it"


def test_the_planted_file_was_actually_outside(outside_tree):
    """Guards the guard: if tmp_path ever landed inside the project tree, every
    escape test above would pass without testing anything."""
    assert not str(pathlib.Path(outside_tree).resolve()).startswith(str(_project_root()))
