"""
tests/test_plugin.py — validate the Claude Code plugin packaging.

Keeps the marketplace/plugin manifests and command files well-formed so a typo
can't silently break `/plugin install`.
"""

import json
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(rel: str) -> dict:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return json.load(f)


def _version() -> str:
    from app import __version__

    return __version__


class TestMarketplace:
    def test_marketplace_valid(self):
        m = _load(".claude-plugin/marketplace.json")
        assert m["name"] == "github-autopilot"
        assert m["plugins"], "marketplace must list at least one plugin"

    def test_plugin_source_exists(self):
        m = _load(".claude-plugin/marketplace.json")
        for p in m["plugins"]:
            src = os.path.join(ROOT, p["source"])
            assert os.path.isdir(src), f"plugin source missing: {p['source']}"
            assert os.path.isfile(os.path.join(src, ".claude-plugin", "plugin.json"))


class TestPluginManifest:
    def test_manifest_valid(self):
        p = _load("plugin/.claude-plugin/plugin.json")
        assert p["name"] == "github-autopilot"
        assert "description" in p and p["description"]

    def test_version_matches_app_ssot(self):
        # Plugin version must track the app's single source of truth.
        p = _load("plugin/.claude-plugin/plugin.json")
        m = _load(".claude-plugin/marketplace.json")
        assert p["version"] == _version()
        assert m["plugins"][0]["version"] == _version()

    def test_mcp_config_points_at_server(self):
        cfg = _load("plugin/.mcp.json")
        srv = cfg["mcpServers"]["github-autopilot"]
        assert srv["type"] == "http"
        assert "${MCP_API_KEY}" in srv["headers"]["Authorization"]  # env, never hardcoded


class TestCommands:
    def test_all_commands_have_frontmatter(self):
        cmd_dir = os.path.join(ROOT, "plugin", "commands")
        files = [f for f in os.listdir(cmd_dir) if f.endswith(".md")]
        assert files, "plugin must ship at least one command"
        for name in files:
            with open(os.path.join(cmd_dir, name), encoding="utf-8") as f:
                text = f.read()
            assert text.startswith("---"), f"{name} missing frontmatter"
            assert "description:" in text, f"{name} missing description"

    def test_commands_reference_mcp_server(self):
        cmd_dir = os.path.join(ROOT, "plugin", "commands")
        for name in os.listdir(cmd_dir):
            if not name.endswith(".md"):
                continue
            with open(os.path.join(cmd_dir, name), encoding="utf-8") as f:
                text = f.read()
            assert "github-autopilot" in text, f"{name} should reference the MCP server"

    @pytest.mark.parametrize("expected", ["review.md", "fix.md", "security.md", "health.md"])
    def test_core_commands_present(self, expected):
        assert os.path.isfile(os.path.join(ROOT, "plugin", "commands", expected))

    def test_no_hardcoded_secret_in_plugin(self):
        # The plugin must never embed a real key/token.
        cfg_raw = open(os.path.join(ROOT, "plugin", ".mcp.json"), encoding="utf-8").read()
        assert "Bearer ${MCP_API_KEY}" in cfg_raw
        # crude check: no long hex/base64 blobs that look like a real token
        import re

        assert not re.search(r"[A-Fa-f0-9]{40,}", cfg_raw)


class TestPluginCommandsMapToRealTools:
    """
    A plugin command is a promise that an MCP tool exists behind it. Nothing
    checked that, so a renamed or removed tool would leave a slash command that
    fails at the moment someone tries it — with no signal until then.
    """

    @staticmethod
    def _commands() -> dict[str, str]:
        import pathlib

        return {
            p.stem: p.read_text(encoding="utf-8")
            for p in pathlib.Path("plugin/commands").glob("*.md")
        }

    def test_every_command_names_a_tool_that_exists(self):
        import re

        from app.mcp.tools import MCP_TOOLS

        names = {t["name"] for t in MCP_TOOLS}
        for stem, body in self._commands().items():
            referenced = set(re.findall(r"`([a-z_]+)`", body)) & (names | {"module"})
            tools = referenced - {"module"}
            assert tools, f"{stem}.md names no MCP tool"
            assert tools <= names, f"{stem}.md references unknown tools: {tools - names}"

    def test_every_command_has_frontmatter(self):
        for stem, body in self._commands().items():
            assert body.startswith("---\n"), f"{stem}.md has no frontmatter"
            assert "description:" in body.split("---")[1], f"{stem}.md has no description"

    def test_the_codebase_map_command_ships(self):
        """Added because codebase_map was the newest and most visual tool and
        had no way to reach it from an IDE."""
        assert "map" in self._commands()
