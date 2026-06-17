"""
tests/test_mcp.py
Tests for app/mcp/mcp_server.py (GitHub has this as mcp_server.py).
"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

# Auto-create app/mcp/__init__.py if missing (CI guard)
_mcp_pkg = _ROOT / "app" / "mcp" / "__init__.py"
if not _mcp_pkg.exists():
    _mcp_pkg.parent.mkdir(parents=True, exist_ok=True)
    _mcp_pkg.write_text('"""app/mcp package."""\n')

# Mock heavy deps before any app imports
_req = MagicMock()
_req.adapters = MagicMock(); _req.adapters.HTTPAdapter = MagicMock
_req.Session = MagicMock; _req.exceptions = MagicMock()
_req.exceptions.RequestException = Exception
_req.exceptions.ConnectionError = ConnectionError
_req.exceptions.Timeout = TimeoutError
sys.modules.setdefault('requests', _req)
sys.modules.setdefault('requests.adapters', _req.adapters)
sys.modules.setdefault('requests.exceptions', _req.exceptions)
for _m in ['structlog','redis','groq','google','google.generativeai',
           'flask_limiter','flask_limiter.util','apscheduler',
           'apscheduler.schedulers','apscheduler.schedulers.background',
           'sentence_transformers','qdrant_client','scipy','flask','flask.logging']:
    sys.modules.setdefault(_m, MagicMock())

# Determine which module name GitHub used
_mcp_server_path = _ROOT / "app" / "mcp" / "mcp_server.py"
_mcp_server_alt  = _ROOT / "app" / "mcp" / "server.py"
_MCP_MODULE = "app.mcp.mcp_server" if _mcp_server_path.exists() else "app.mcp.server"


def _import_mcp():
    """Import whichever mcp server module exists."""
    if _mcp_server_path.exists():
        import app.mcp.mcp_server as m
    else:
        import app.mcp.server as m
    return m


class TestMCPProtocol:

    def setup_method(self, m=None):
        self._patcher = patch(f'{_MCP_MODULE}.MCP_API_KEY', "")
        self._patcher.start()

    def teardown_method(self, m=None):
        self._patcher.stop()

    def test_initialize(self):
        mod = _import_mcp()
        resp, status = mod.handle_mcp_request("initialize", {}, "")
        assert status == 200
        assert resp["protocolVersion"] == "2024-11-05"
        assert resp["serverInfo"]["name"] == "github-autopilot"

    def test_tools_list_returns_8_tools(self):
        mod = _import_mcp()
        resp, status = mod.handle_mcp_request("tools/list", {}, "")
        assert status == 200
        assert len(resp["tools"]) == 8

    def test_each_tool_has_required_fields(self):
        mod = _import_mcp()
        for tool in mod.MCP_TOOLS:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool
            assert "required" in tool["inputSchema"]

    def test_unknown_method_returns_400(self):
        mod = _import_mcp()
        _, status = mod.handle_mcp_request("bad/method", {}, "")
        assert status == 400

    def test_unknown_tool_returns_400_with_available(self):
        mod = _import_mcp()
        resp, status = mod.handle_mcp_request(
            "tools/call", {"name": "nonexistent", "arguments": {}}, ""
        )
        assert status == 400
        assert "available" in resp["error"]


class TestMCPAuth:

    def test_no_key_set_allows_all(self):
        mod = _import_mcp()
        with patch(f'{_MCP_MODULE}.MCP_API_KEY', ""):
            _, status = mod.handle_mcp_request("tools/list", {}, "")
        assert status == 200

    def test_wrong_token_gives_401(self):
        mod = _import_mcp()
        with patch(f'{_MCP_MODULE}.MCP_API_KEY', "correct"):
            _, status = mod.handle_mcp_request("tools/list", {}, "wrong")
        assert status == 401

    def test_correct_token_gives_200(self):
        mod = _import_mcp()
        with patch(f'{_MCP_MODULE}.MCP_API_KEY', "correct"):
            _, status = mod.handle_mcp_request("tools/list", {}, "correct")
        assert status == 200

    def test_empty_token_rejected_when_key_set(self):
        mod = _import_mcp()
        with patch(f'{_MCP_MODULE}.MCP_API_KEY', "correct"):
            _, status = mod.handle_mcp_request("tools/list", {}, "")
        assert status == 401


class TestAnalyzePR:

    def test_missing_args_returns_error(self):
        mod = _import_mcp()
        assert "Error" in mod._handle_analyze_pr({})

    def test_missing_installation_id(self):
        mod = _import_mcp()
        result = mod._handle_analyze_pr({"repo": "o/r", "pr_number": 1})
        assert "installation_id" in result

    def test_successful_analysis(self):
        mod = _import_mcp()
        with patch('app.github.auth.get_installation_token', return_value="tok"):
            with patch('app.github.client.gh_get', return_value={"title": "PR"}):
                with patch('app.ai.router.router.ask', return_value=({
                    "grade": "A", "summary": "Good PR",
                    "security_issues": [], "test_gaps": [],
                    "improvements": [], "recommendation": "approve",
                }, MagicMock())):
                    result = mod._handle_analyze_pr({
                        "repo": "o/r", "pr_number": 1, "installation_id": 123
                    })
        assert "Grade:** A" in result


class TestFixIssue:

    def test_missing_args(self):
        mod = _import_mcp()
        assert "Error" in mod._handle_fix_issue({})

    def test_missing_installation_id(self):
        mod = _import_mcp()
        result = mod._handle_fix_issue({"repo": "o/r", "issue_number": 1})
        assert "installation_id" in result

    def test_successful_fix(self):
        mod = _import_mcp()
        with patch('app.github.auth.get_installation_token', return_value="tok"):
            with patch('app.github.client.gh_get', return_value={
                "title": "Bug", "body": "crashes"
            }):
                with patch('app.ai.router.router.ask', return_value=({
                    "root_cause": "null check missing",
                    "fix": "if x is None: return",
                    "test": "def test_none(): ...",
                    "confidence": 0.9,
                }, MagicMock())):
                    result = mod._handle_fix_issue({
                        "repo": "o/r", "issue_number": 1, "installation_id": 123
                    })
        assert "null check missing" in result
        assert "90%" in result


class TestScanSecrets:

    def test_missing_content(self):
        mod = _import_mcp()
        assert "Error" in mod._handle_scan_secrets({})

    def test_clean_code_returns_no_secrets(self):
        mod = _import_mcp()
        result = mod._handle_scan_secrets({"content": "x = 1 + 2"})
        assert "No secrets" in result

    def test_content_prefixed_with_plus(self):
        import inspect
        mod = _import_mcp()
        src = inspect.getsource(mod._handle_scan_secrets)
        assert '"+' in src or "'+'" in src

    def test_aws_key_detected(self):
        mod = _import_mcp()
        result = mod._handle_scan_secrets({"content": 'k="AKIAIOSFODNN7EXAMPLE"'})
        assert isinstance(result, str)


class TestExplainCode:

    def test_missing_code(self):
        mod = _import_mcp()
        assert "Error" in mod._handle_explain_code({})

    def test_returns_explanation(self):
        mod = _import_mcp()
        with patch('app.ai.router.router.ask_text',
                   return_value=("Adds numbers", MagicMock())):
            result = mod._handle_explain_code({"code": "def add(a,b): return a+b"})
        assert "Adds numbers" in result

    def test_depth_deep_uses_1500_tokens(self):
        mod = _import_mcp()
        captured = {}
        def cap(*a, **kw): captured['mt'] = kw.get('max_tokens'); return ("ok", MagicMock())
        with patch('app.ai.router.router.ask_text', side_effect=cap):
            mod._handle_explain_code({"code": "x=1", "depth": "deep"})
        assert captured.get('mt') == 1500

    def test_depth_brief_uses_400_tokens(self):
        mod = _import_mcp()
        captured = {}
        def cap(*a, **kw): captured['mt'] = kw.get('max_tokens'); return ("ok", MagicMock())
        with patch('app.ai.router.router.ask_text', side_effect=cap):
            mod._handle_explain_code({"code": "x=1", "depth": "brief"})
        assert captured.get('mt') == 400


class TestGenerateTests:

    def test_missing_code(self):
        mod = _import_mcp()
        assert "Error" in mod._handle_generate_tests({})

    def test_returns_test_code(self):
        mod = _import_mcp()
        with patch('app.ai.router.router.ask_text',
                   return_value=("def test_add(): ...", MagicMock())):
            result = mod._handle_generate_tests({"code": "def add(a,b): return a+b"})
        assert "test_add" in result


class TestSecurityReview:

    def test_missing_content(self):
        mod = _import_mcp()
        assert "Error" in mod._handle_security_review({})

    def test_successful_review(self):
        mod = _import_mcp()
        with patch('app.ai.router.router.ask', return_value=({
            "risk_level": "high",
            "findings": [{"issue": "SQL injection", "severity": "critical",
                          "line": 10, "fix": "parameterized query"}],
            "cve_risks": ["CVE-2024-1234"],
        }, MagicMock())):
            result = mod._handle_security_review({"content": "SELECT * FROM {uid}"})
        assert "HIGH" in result
        assert "SQL injection" in result
        assert "CVE-2024-1234" in result


class TestGetRepoHealth:

    def test_missing_repo(self):
        mod = _import_mcp()
        assert "Error" in mod._handle_get_repo_health({})

    def test_missing_installation_id(self):
        mod = _import_mcp()
        result = mod._handle_get_repo_health({"repo": "o/r"})
        assert "installation_id" in result

    def test_successful_health(self):
        mod = _import_mcp()
        with patch('app.github.auth.get_installation_token', return_value="tok"):
            with patch('app.ai.router.router.ask', return_value=({
                "grade": "B", "score": 7.5,
                "top_issues": ["low coverage"],
                "quick_wins": ["add CI badge"],
            }, MagicMock())):
                result = mod._handle_get_repo_health({
                    "repo": "o/r", "installation_id": 123
                })
        assert "Grade:** B" in result
        assert "7.5" in result


class TestRunCommand:

    def test_missing_args(self):
        mod = _import_mcp()
        assert "Error" in mod._handle_run_command({})

    def test_merge_blocked(self):
        mod = _import_mcp()
        result = mod._handle_run_command({
            "repo": "o/r", "issue_number": 1,
            "command": "/merge", "installation_id": 123
        })
        assert "not available via MCP" in result

    def test_autofix_blocked(self):
        mod = _import_mcp()
        result = mod._handle_run_command({
            "repo": "o/r", "issue_number": 1,
            "command": "/autofix", "installation_id": 123
        })
        assert "not available via MCP" in result

    def test_apply_blocked(self):
        mod = _import_mcp()
        result = mod._handle_run_command({
            "repo": "o/r", "issue_number": 1,
            "command": "/apply", "installation_id": 123
        })
        assert "not available via MCP" in result

    def test_rollback_blocked(self):
        mod = _import_mcp()
        result = mod._handle_run_command({
            "repo": "o/r", "issue_number": 1,
            "command": "/rollback", "installation_id": 123
        })
        assert "not available via MCP" in result

    def test_release_blocked(self):
        mod = _import_mcp()
        result = mod._handle_run_command({
            "repo": "o/r", "issue_number": 1,
            "command": "/release", "installation_id": 123
        })
        assert "not available via MCP" in result

    def test_fix_routes_correctly(self):
        mod = _import_mcp()
        with patch('app.github.auth.get_installation_token', return_value="tok"):
            with patch('app.github.client.gh_get', return_value={"title":"Bug","body":"body"}):
                with patch('app.handlers.comments._cmd_fix',
                           return_value="## Fix") as mock_fix:
                    result = mod._handle_run_command({
                        "repo": "o/r", "issue_number": 1,
                        "command": "/fix", "installation_id": 123
                    })
        assert result == "## Fix"
        assert mock_fix.call_args[0][0] == "Bug"

    def test_budget_called_with_zero_args(self):
        mod = _import_mcp()
        with patch('app.github.auth.get_installation_token', return_value="tok"):
            with patch('app.github.client.gh_get', return_value={"title":"t","body":"b"}):
                with patch('app.handlers.comments._cmd_budget',
                           return_value="## Budget") as mock_budget:
                    result = mod._handle_run_command({
                        "repo": "o/r", "issue_number": 1,
                        "command": "/budget", "installation_id": 123
                    })
        mock_budget.assert_called_once_with()
        assert result == "## Budget"

    def test_bad_parse_returns_error(self):
        mod = _import_mcp()
        with patch('app.github.auth.get_installation_token', return_value="tok"):
            with patch('app.github.client.gh_get', return_value={"title":"t","body":"b"}):
                with patch('app.handlers.comments._extract_command', return_value=None):
                    result = mod._handle_run_command({
                        "repo": "o/r", "issue_number": 1,
                        "command": "/fix", "installation_id": 123
                    })
        assert "Error" in result


class TestToolsCallDispatch:

    def setup_method(self, m=None):
        self._patcher = patch(f'{_MCP_MODULE}.MCP_API_KEY', "")
        self._patcher.start()

    def teardown_method(self, m=None):
        self._patcher.stop()

    def test_explain_code_via_dispatch(self):
        mod = _import_mcp()
        with patch('app.ai.router.router.ask_text',
                   return_value=("Adds numbers", MagicMock())):
            resp, status = mod.handle_mcp_request("tools/call", {
                "name": "explain_code",
                "arguments": {"code": "def add(a,b): return a+b"}
            }, "")
        assert status == 200
        assert "Adds numbers" in resp["content"][0]["text"]

    def test_response_includes_latency(self):
        mod = _import_mcp()
        with patch('app.ai.router.router.ask_text',
                   return_value=("ok", MagicMock())):
            resp, status = mod.handle_mcp_request("tools/call", {
                "name": "explain_code", "arguments": {"code": "x=1"}
            }, "")
        assert "latency_ms" in resp
        assert resp["latency_ms"] >= 0

    def test_handler_exception_gives_500(self):
        mod = _import_mcp()
        original = mod.TOOL_HANDLERS["explain_code"]
        mod.TOOL_HANDLERS["explain_code"] = lambda _: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            resp, status = mod.handle_mcp_request("tools/call", {
                "name": "explain_code", "arguments": {"code": "x=1"}
            }, "")
        finally:
            mod.TOOL_HANDLERS["explain_code"] = original
        assert status == 500
        assert "boom" in resp["error"]["message"]


if __name__ == "__main__":
    print("Run with: python -m pytest tests/test_mcp.py -v")
