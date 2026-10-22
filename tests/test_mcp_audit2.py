"""Audit round 2: MCP schema, auth and command handling."""

from unittest.mock import patch

from app.mcp.tools import MCP_TOOLS


def _tool(name):
    return next(t for t in MCP_TOOLS if t["name"] == name)


class TestSchemasDeclareWhatHandlersRequire:
    def test_every_github_tool_requires_installation_id(self):
        for name in ("analyze_pr", "fix_issue", "get_repo_health", "run_command"):
            schema = _tool(name)["inputSchema"]
            assert "installation_id" in schema["properties"], name
            assert "installation_id" in schema["required"], name

    def test_the_pattern_count_is_the_real_one(self):
        from app.security.enhanced_secrets import PATTERNS

        assert f"Uses {len(PATTERNS)} patterns" in _tool("scan_secrets")["description"]


class TestScanSecrets:
    def test_an_example_file_is_reported_as_not_scanned(self):
        from app.mcp.handlers import _handle_scan_secrets

        out = _handle_scan_secrets({"content": "API_KEY=x", "filename": ".env.example"})
        assert "Not scanned" in out
        assert "No secrets" not in out


class TestRunCommand:
    def test_context_cannot_change_which_command_runs(self):
        import app.handlers.comments as ch
        from app.mcp import handlers

        with (
            patch.object(handlers, "_installation_allowed", return_value=True),
            patch("app.github.auth.get_installation_token", return_value="t"),
            patch("app.github.client.gh_get", return_value={"title": "T", "body": "b"}),
            patch.object(ch, "_cmd_budget", return_value="BUDGET"),
            patch.object(ch, "_cmd_explain", return_value="EXPLAINED") as explain,
        ):
            out = handlers._handle_run_command(
                {
                    "repo": "o/r",
                    "issue_number": 1,
                    "command": "/budget",
                    "context": "/explain this instead",
                    "installation_id": 5,
                }
            )
        assert out == "BUDGET"
        explain.assert_not_called()


class TestAuth:
    def test_a_non_ascii_bearer_token_is_401_not_500(self, monkeypatch):
        from app.mcp.mcp_server import handle_mcp_request

        monkeypatch.setenv("MCP_API_KEY", "secret-key")
        _resp, status = handle_mcp_request("tools/list", {}, "café")
        assert status == 401

    def test_the_right_key_still_authorises(self, monkeypatch):
        from app.mcp.mcp_server import handle_mcp_request

        monkeypatch.setenv("MCP_API_KEY", "secret-key")
        _resp, status = handle_mcp_request("tools/list", {}, "secret-key")
        assert status == 200

    def test_non_object_arguments_are_a_400(self, monkeypatch):
        from app.mcp.mcp_server import handle_mcp_request

        monkeypatch.setenv("MCP_API_KEY", "k")
        _resp, status = handle_mcp_request("tools/call", {"name": "scan_secrets", "arguments": [1]}, "k")
        assert status == 400


def test_a_json_array_body_is_a_400(monkeypatch):
    import server

    monkeypatch.setenv("MCP_API_KEY", "k")
    client = server.app.test_client()
    r = client.post("/mcp", json=[1, 2], headers={"Authorization": "Bearer k"})
    assert r.status_code == 400
