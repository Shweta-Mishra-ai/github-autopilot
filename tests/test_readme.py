"""
tests/test_readme.py

Managed README regions.

The design constraint these enforce: the bot rewrites only what sits between
its own markers and never touches hand-written prose. An LLM regenerating a
good README makes it worse, so this deliberately does not do that — it
regenerates blocks whose content the code already knows, and delivers them as a
pull request rather than a commit to the default branch.
"""

import base64
from unittest.mock import MagicMock, patch

import pytest

from app.handlers import readme as R
from app.github.client import GitHubError


def _doc(body_stats="", body_arch=""):
    return (
        "# My Project\n\n"
        "Hand-written intro that must survive untouched.\n\n"
        "<!-- autopilot:stats:start -->"
        f"{body_stats}"
        "<!-- autopilot:stats:end -->\n\n"
        "More hand-written prose.\n\n"
        "<!-- autopilot:architecture:start -->"
        f"{body_arch}"
        "<!-- autopilot:architecture:end -->\n\n"
        "Closing prose.\n"
    )


@pytest.fixture
def renderers():
    return {"stats": lambda: "GENERATED STATS", "architecture": lambda: "GENERATED ARCH"}


@pytest.fixture
def log_ctx():
    return MagicMock()


@pytest.fixture
def config():
    c = MagicMock()
    c.footer = "\n\n---\n*bot*"
    return c


class TestRegionDetection:
    def test_finds_present_regions(self):
        assert set(R.find_regions(_doc())) == {"stats", "architecture"}

    def test_finds_nothing_in_a_plain_readme(self):
        assert R.find_regions("# Just a README\n\nNo markers here.\n") == []

    def test_tolerates_whitespace_in_markers(self):
        text = "<!--   autopilot:stats:start   -->\n\n<!--  autopilot:stats:end  -->"
        assert "stats" in R.find_regions(text)


class TestApplyRegions:
    def test_fills_empty_regions(self, renderers):
        out, changed = R.apply_regions(_doc(), renderers)
        assert "GENERATED STATS" in out
        assert "GENERATED ARCH" in out
        assert set(changed) == {"stats", "architecture"}

    def test_preserves_prose_outside_markers(self, renderers):
        out, _ = R.apply_regions(_doc(), renderers)
        assert "Hand-written intro that must survive untouched." in out
        assert "More hand-written prose." in out
        assert "Closing prose." in out

    def test_markers_survive_the_rewrite(self, renderers):
        out, _ = R.apply_regions(_doc(), renderers)
        assert "<!-- autopilot:stats:start -->" in out
        assert "<!-- autopilot:stats:end -->" in out

    def test_replaces_stale_content(self, renderers):
        out, changed = R.apply_regions(_doc(body_stats="\nOLD AND WRONG\n"), renderers)
        assert "OLD AND WRONG" not in out
        assert "GENERATED STATS" in out
        assert "stats" in changed

    def test_is_idempotent(self, renderers):
        once, _ = R.apply_regions(_doc(), renderers)
        twice, changed = R.apply_regions(once, renderers)
        assert twice == once
        assert changed == [], "a second run must produce no diff"

    def test_absent_region_is_not_invented(self, renderers):
        text = "# Doc\n\n<!-- autopilot:stats:start -->\n<!-- autopilot:stats:end -->\n"
        out, changed = R.apply_regions(text, renderers)
        assert changed == ["stats"]
        assert "GENERATED ARCH" not in out

    def test_no_markers_means_no_edits(self, renderers):
        text = "# Nothing to do here\n"
        out, changed = R.apply_regions(text, renderers)
        assert out == text
        assert changed == []

    def test_failing_renderer_leaves_its_region_alone(self):
        def boom():
            raise RuntimeError("generator exploded")

        out, changed = R.apply_regions(
            _doc(body_stats="\nEXISTING\n"),
            {"stats": boom, "architecture": lambda: "ARCH"},
        )
        assert "EXISTING" in out, "a broken generator must not blank the section"
        assert changed == ["architecture"]

    def test_backslashes_in_generated_content_survive(self):
        """re.sub would interpret these in the replacement string."""
        out, _ = R.apply_regions(_doc(), {"stats": lambda: r"C:\path\to\thing"})
        assert r"C:\path\to\thing" in out

    def test_group_reference_syntax_in_content_survives(self):
        out, _ = R.apply_regions(_doc(), {"stats": lambda: r"literal \g<0> text"})
        assert r"literal \g<0> text" in out


class TestRenderers:
    def test_stats_are_derived_not_hardcoded(self):
        from app.handlers.comments.constants import ALL_COMMANDS
        from app.mcp.tools import MCP_TOOLS

        out = R.render_stats()
        assert f"| Slash commands | {len(ALL_COMMANDS)} |" in out
        assert f"| MCP tools | {len(MCP_TOOLS)} |" in out

    def test_commands_table_covers_the_registry(self):
        from app.handlers.comments.constants import ALL_COMMANDS

        out = R.render_commands()
        for cmd in ALL_COMMANDS:
            assert f"`{cmd}`" in out

    def test_commands_table_marks_restricted_commands(self):
        out = R.render_commands()
        assert "| `/merge` | maintainer |" in out
        assert "| `/explain` | anyone |" in out

    def test_architecture_is_a_mermaid_block(self):
        out = R.render_architecture()
        assert out.startswith("```mermaid")
        assert out.rstrip().endswith("```")
        assert "graph LR" in out


class TestDelivery:
    @pytest.fixture
    def gh(self):
        with (
            patch.object(R, "gh_get") as get,
            patch.object(R, "gh_post") as post,
            patch.object(R, "gh_put") as put,
        ):
            yield {"get": get, "post": post, "put": put}

    @pytest.fixture
    def enabled(self, monkeypatch):
        monkeypatch.setenv("README_SELF_UPDATE_REPO", "o/r")

    def _readme_response(self, text):
        return {"content": base64.b64encode(text.encode()).decode(), "sha": "blobsha"}

    def test_disabled_unless_env_names_this_repo(self, gh, config, log_ctx, monkeypatch):
        """The renderers read the local source tree, so running against an
        arbitrary repository would describe the bot, not that repository."""
        monkeypatch.delenv("README_SELF_UPDATE_REPO", raising=False)
        assert R.maybe_update_readme("o/r", [], "tok", config, log_ctx) is False
        gh["get"].assert_not_called()

    def test_wrong_repo_is_skipped(self, gh, config, log_ctx, monkeypatch):
        monkeypatch.setenv("README_SELF_UPDATE_REPO", "someone/else")
        assert R.maybe_update_readme("o/r", [], "tok", config, log_ctx) is False
        gh["get"].assert_not_called()

    def test_readme_without_markers_is_left_alone(self, gh, enabled, config, log_ctx):
        gh["get"].return_value = self._readme_response("# Plain readme\n")
        assert R.maybe_update_readme("o/r", [], "tok", config, log_ctx) is False
        gh["put"].assert_not_called()
        gh["post"].assert_not_called()

    def test_current_readme_opens_no_pr(self, gh, enabled, config, log_ctx):
        rendered, _ = R.apply_regions(_doc())
        gh["get"].return_value = self._readme_response(rendered)
        assert R.maybe_update_readme("o/r", [], "tok", config, log_ctx) is False
        gh["post"].assert_not_called()

    def test_unreadable_readme_is_not_an_error(self, gh, enabled, config, log_ctx):
        gh["get"].side_effect = GitHubError("Not found", 404)
        assert R.maybe_update_readme("o/r", [], "tok", config, log_ctx) is False

    def test_opens_a_pr_when_regions_drift(self, gh, enabled, config, log_ctx):
        def _route(path, token, *a, **kw):
            if "contents/README.md" in path:
                return self._readme_response(_doc(body_stats="\nSTALE\n"))
            if path.endswith("/pulls?head=o:" + R.BRANCH + "&state=open&per_page=1"):
                return []
            if "/pulls?" in path:
                return []
            if "git/ref/heads/" in path:
                return {"object": {"sha": "basesha"}}
            return {"default_branch": "main"}

        gh["get"].side_effect = _route
        gh["post"].return_value = {"number": 42}
        with patch.object(R, "_recently_proposed", return_value=False):
            assert R.maybe_update_readme("o/r", [], "tok", config, log_ctx) is True

        # A branch ref and a PR, never a commit to the default branch.
        posted = [c.args[0] for c in gh["post"].call_args_list]
        assert any("git/refs" in p for p in posted)
        assert any(p.endswith("/pulls") for p in posted)
        put_body = gh["put"].call_args[0][2]
        assert put_body["branch"] == R.BRANCH

    def test_never_commits_to_the_default_branch(self, gh, enabled, config, log_ctx):
        def _route(path, token, *a, **kw):
            if "contents/README.md" in path:
                return self._readme_response(_doc(body_stats="\nSTALE\n"))
            if "/pulls?" in path:
                return []
            if "git/ref/heads/" in path:
                return {"object": {"sha": "basesha"}}
            return {"default_branch": "main"}

        gh["get"].side_effect = _route
        gh["post"].return_value = {"number": 1}
        with patch.object(R, "_recently_proposed", return_value=False):
            R.maybe_update_readme("o/r", [], "tok", config, log_ctx)
        assert gh["put"].call_args[0][2]["branch"] != "main"

    def test_open_pr_blocks_a_second_one(self, gh, enabled, config, log_ctx):
        gh["get"].return_value = self._readme_response(_doc(body_stats="\nSTALE\n"))
        with patch.object(R, "_pr_already_open", return_value=True):
            assert R.maybe_update_readme("o/r", [], "tok", config, log_ctx) is False
        gh["post"].assert_not_called()

    def test_recent_proposal_blocks_a_second_one(self, gh, enabled, config, log_ctx):
        gh["get"].return_value = self._readme_response(_doc(body_stats="\nSTALE\n"))
        with (
            patch.object(R, "_pr_already_open", return_value=False),
            patch.object(R, "_recently_proposed", return_value=True),
        ):
            assert R.maybe_update_readme("o/r", [], "tok", config, log_ctx) is False

    def test_unexpected_error_never_propagates(self, gh, enabled, config, log_ctx):
        """Runs on the push path beside the secret and dependency scans."""
        gh["get"].side_effect = RuntimeError("boom")
        assert R.maybe_update_readme("o/r", [], "tok", config, log_ctx) is False


class TestGuards:
    def test_pr_lookup_failure_fails_closed(self):
        """Not knowing whether a PR is open is a reason not to open a second."""
        with patch.object(R, "gh_get", side_effect=RuntimeError("api down")):
            assert R._pr_already_open("o/r", "tok") is True

    def test_dedup_fails_closed_on_redis_error(self):
        with patch("app.core.redis_client.get_redis", side_effect=OSError("down")):
            assert R._recently_proposed("o/r") is True

    def test_default_branch_falls_back_to_main(self):
        with patch.object(R, "gh_get", side_effect=RuntimeError("x")):
            assert R._default_branch("o/r", "tok") == "main"


class TestWiring:
    def test_push_handler_calls_it(self):
        import inspect

        import app.handlers.push as push_mod

        assert "maybe_update_readme" in inspect.getsource(push_mod.handle)

    def test_config_default_is_declared(self):
        from app.core.config import DEFAULTS

        assert DEFAULTS["push"]["update_readme"] is True


class TestThisRepositorysReadme:
    def test_readme_regions_are_current(self):
        """Fails when README.md's generated blocks drift from the code — the
        exact rot this feature exists to prevent (it claimed "8 tools" while
        nine were exposed)."""
        with open("README.md", encoding="utf-8") as fh:
            current = fh.read()
        _, changed = R.apply_regions(current)
        assert changed == [], (
            f"README regions are stale: {changed}. Regenerate with "
            f"python -m app.handlers.readme"
        )


class TestRunsWithoutThirdPartyDependencies:
    """
    The `Codebase map` CI job deliberately installs no dependencies — the AST
    extractor is pure stdlib — and runs both `codegraph` and
    `python -m app.handlers.readme --check` there.

    A module-level `from app.github.client import ...` in readme.py broke that
    job with ModuleNotFoundError: no module named 'requests'. It passed locally
    because a dev venv has everything installed, so only CI could catch it.
    These tests catch it instead.
    """

    BLOCKED = frozenset({"requests", "flask", "redis", "groq", "jwt", "cryptography"})

    @staticmethod
    def _import_with_blocked_deps(module_name, blocked):
        """Import `module_name` in a subprocess with `blocked` unimportable."""
        import subprocess
        import sys
        import textwrap

        script = textwrap.dedent(f"""
            import builtins, sys
            BLOCKED = {set(blocked)!r}
            _real = builtins.__import__
            def guard(name, *a, **kw):
                root = name.split(".")[0]
                if root in BLOCKED:
                    raise ModuleNotFoundError("No module named " + repr(root))
                return _real(name, *a, **kw)
            builtins.__import__ = guard
            import {module_name}  # noqa: F401
            print("IMPORT_OK")
        """)
        return subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
        )

    def test_codegraph_imports_without_third_party_deps(self):
        r = self._import_with_blocked_deps("app.intelligence.codegraph", self.BLOCKED)
        assert "IMPORT_OK" in r.stdout, r.stderr[-1500:]

    def test_readme_module_imports_without_third_party_deps(self):
        r = self._import_with_blocked_deps("app.handlers.readme", self.BLOCKED)
        assert "IMPORT_OK" in r.stdout, (
            "app/handlers/readme.py imports a third-party package at module "
            "scope. The CI job that runs `--check` installs no dependencies.\n"
            + r.stderr[-1500:]
        )

    def test_command_registry_imports_without_third_party_deps(self):
        """The registry moved to app/core/commands.py for exactly this reason:
        importing app.handlers.comments.constants executes that package's
        __init__, which pulls in the GitHub + JWT stack."""
        r = self._import_with_blocked_deps("app.core.commands", self.BLOCKED)
        assert "IMPORT_OK" in r.stdout, r.stderr[-1500:]

    def test_every_renderer_runs_without_third_party_deps(self):
        import subprocess
        import sys
        import textwrap

        script = textwrap.dedent(f"""
            import builtins
            BLOCKED = {set(self.BLOCKED)!r}
            _real = builtins.__import__
            def guard(name, *a, **kw):
                root = name.split(".")[0]
                if root in BLOCKED:
                    raise ModuleNotFoundError("No module named " + repr(root))
                return _real(name, *a, **kw)
            builtins.__import__ = guard
            from app.handlers.readme import REGION_RENDERERS
            for name, fn in REGION_RENDERERS.items():
                assert fn().strip(), name
            print("RENDER_OK")
        """)
        r = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
        )
        assert "RENDER_OK" in r.stdout, r.stderr[-1500:]

    def test_check_cli_runs_without_third_party_deps(self):
        """End-to-end: exactly what the CI step invokes."""
        import subprocess
        import sys
        import textwrap

        script = textwrap.dedent(f"""
            import builtins
            BLOCKED = {set(self.BLOCKED)!r}
            _real = builtins.__import__
            def guard(name, *a, **kw):
                root = name.split(".")[0]
                if root in BLOCKED:
                    raise ModuleNotFoundError("No module named " + repr(root))
                return _real(name, *a, **kw)
            builtins.__import__ = guard
            from app.handlers.readme import main
            code = main(["README.md", "--check"])
            print("CHECK_EXIT", code)
        """)
        r = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
        )
        assert "CHECK_EXIT 0" in r.stdout, (
            "python -m app.handlers.readme --check did not succeed with no "
            "third-party deps installed — this is what CI runs.\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr[-1500:]}"
        )


class TestRegistryIsSingleSourced:
    def test_constants_reexports_the_same_object(self):
        from app.core.commands import ALL_COMMANDS as canonical
        from app.handlers.comments.constants import ALL_COMMANDS as reexport

        assert reexport is canonical, "a copy would drift; it must be the same object"

    def test_authorization_reexports_the_same_object(self):
        from app.core.authorization import RESTRICTED_COMMANDS as reexport
        from app.core.commands import RESTRICTED_COMMANDS as canonical

        assert reexport is canonical

    def test_every_restricted_command_is_a_real_command(self):
        """A restricted command absent from the registry is unreachable, so its
        restriction is silently meaningless."""
        from app.core.commands import ALL_COMMANDS, RESTRICTED_COMMANDS

        assert RESTRICTED_COMMANDS <= set(ALL_COMMANDS)
