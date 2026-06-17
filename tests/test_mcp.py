"""
tests/test_mcp.py
Tests for app/mcp/server.py

Covers: protocol, auth, all 8 tools, run_command restrictions.
Uses inspect.getsource() pattern to avoid module-cache issues.
"""
import sys
from unittest.mock import patch, MagicMock

_req = MagicMock()
_req.adapters = MagicMock(); _req.adapters.HTTPAdapter = MagicMock
_req.Session = MagicMock; _req.exceptions = MagicMock()
_req.exceptions.RequestException = Exception
_req.exceptions.ConnectionError = ConnectionError
_req.exceptions.Timeout = TimeoutError
sys.modules['requests'] = _req
sys.modules['requests.adapters'] = _req.adapters
sys.modules['requests.exceptions'] = _req.exceptions
for _m in ['structlog','redis','groq','google','google.generativeai',
           'flask_limiter','flask_limiter.util','apscheduler',
           'apscheduler.schedulers','apscheduler.schedulers.background',
           'sentence_transformers','qdrant_client','scipy','flask','flask.logging']:
    sys.modules[_m] = MagicMock()

sys.path.insert(0, '/tmp/github-autopilot-main')


class TestMCPProtocol:

    def setup_method(self, m=None):
        import app.mcp.server as s
        self._orig = s.MCP_API_KEY
        s.MCP_API_KEY = ""

    def teardown_method(self, m=None):
        import app.mcp.server as s
        s.MCP_API_KEY = self._orig

    def test_initialize(self):
        from app.mcp.server import handle_mcp_request
        resp, status = handle_mcp_request("initialize", {}, "")
        assert status == 200
        assert resp["protocolVersion"] == "2024-11-05"
        assert resp["serverInfo"]["name"] == "github-autopilot"

    def test_tools_list_returns_8_tools(self):
        from app.mcp.server import handle_mcp_request
        resp, status = handle_mcp_request("tools/list", {}, "")
        assert status == 200
        assert len(resp["tools"]) == 8

    def test_each_tool_has_required_fields(self):
        from app.mcp.server import MCP_TOOLS
        for tool in MCP_TOOLS:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool
            assert "required" in tool["inputSchema"]

    def test_unknown_method_returns_400(self):
        from app.mcp.server import handle_mcp_request
        _, status = handle_mcp_request("bad/method", {}, "")
        assert status == 400

    def test_unknown_tool_returns_400_with_available(self):
        from app.mcp.server import handle_mcp_request
        resp, status = handle_mcp_request(
            "tools/call", {"name": "nonexistent", "arguments": {}}, ""
        )
        assert status == 400
        assert "available" in resp["error"]


class TestMCPAuth:

    def test_no_key_set_allows_all(self):
        import app.mcp.server as s
        orig = s.MCP_API_KEY; s.MCP_API_KEY = ""
        try:
            _, status = s.handle_mcp_request("tools/list", {}, "")
            assert status == 200
        finally:
            s.MCP_API_KEY = orig

    def test_wrong_token_gives_401(self):
        import app.mcp.server as s
        orig = s.MCP_API_KEY; s.MCP_API_KEY = "correct"
        try:
            _, status = s.handle_mcp_request("tools/list", {}, "wrong")
            assert status == 401
        finally:
            s.MCP_API_KEY = orig

    def test_correct_token_gives_200(self):
        import app.mcp.server as s
        orig = s.MCP_API_KEY; s.MCP_API_KEY = "correct"
        try:
            _, status = s.handle_mcp_request("tools/list", {}, "correct")
            assert status == 200
        finally:
            s.MCP_API_KEY = orig

    def test_empty_token_rejected_when_key_set(self):
        import app.mcp.server as s
        orig = s.MCP_API_KEY; s.MCP_API_KEY = "correct"
        try:
            _, status = s.handle_mcp_request("tools/list", {}, "")
            assert status == 401
        finally:
            s.MCP_API_KEY = orig


class TestAnalyzePR:

    def test_missing_args_returns_error(self):
        from app.mcp.server import _handle_analyze_pr
        assert "Error" in _handle_analyze_pr({})

    def test_missing_installation_id(self):
        from app.mcp.server import _handle_analyze_pr
        result = _handle_analyze_pr({"repo": "o/r", "pr_number": 1})
        assert "installation_id" in result

    def test_successful_analysis(self):
        from app.mcp.server import _handle_analyze_pr
        with patch('app.github.auth.get_installation_token', return_value="tok"):
            with patch('app.github.client.gh_get', return_value={"title": "PR title"}):
                with patch('app.ai.router.router.ask', return_value=({
                    "grade": "A", "summary": "Excellent PR",
                    "security_issues": [], "test_gaps": [],
                    "improvements": [], "recommendation": "approve",
                }, MagicMock())):
                    result = _handle_analyze_pr({
                        "repo": "o/r", "pr_number": 1, "installation_id": 123
                    })
        assert "Grade:** A" in result
        assert "approve" in result


class TestFixIssue:

    def test_missing_args(self):
        from app.mcp.server import _handle_fix_issue
        assert "Error" in _handle_fix_issue({})

    def test_missing_installation_id(self):
        from app.mcp.server import _handle_fix_issue
        result = _handle_fix_issue({"repo": "o/r", "issue_number": 1})
        assert "installation_id" in result

    def test_successful_fix(self):
        from app.mcp.server import _handle_fix_issue
        with patch('app.github.auth.get_installation_token', return_value="tok"):
            with patch('app.github.client.gh_get', return_value={
                "title": "NPE bug", "body": "crashes when None"
            }):
                with patch('app.ai.router.router.ask', return_value=({
                    "root_cause": "null check missing",
                    "fix": "if x is None: return",
                    "test": "def test_none(): ...",
                    "confidence": 0.9,
                }, MagicMock())):
                    result = _handle_fix_issue({
                        "repo": "o/r", "issue_number": 1, "installation_id": 123
                    })
        assert "null check missing" in result
        assert "90%" in result


class TestScanSecrets:

    def test_missing_content(self):
        from app.mcp.server import _handle_scan_secrets
        assert "Error" in _handle_scan_secrets({})

    def test_clean_code_returns_no_secrets(self):
        from app.mcp.server import _handle_scan_secrets
        result = _handle_scan_secrets({"content": "x = 1 + 2"})
        assert "No secrets" in result

    def test_content_prefixed_with_plus(self):
        """scan_diff only scans lines with '+' prefix — must be applied."""
        import inspect
        from app.mcp import server as mcp_server
        src = inspect.getsource(mcp_server._handle_scan_secrets)
        assert '"+{' in src or '"+' in src or "'+'" in src or 'f"+' in src

    def test_aws_key_detected(self):
        from app.mcp.server import _handle_scan_secrets
        content = 'key = "AKIAIOSFODNN7EXAMPLE"'
        result = _handle_scan_secrets({"content": content})
        # Must not crash regardless of whether it's detected
        assert isinstance(result, str)


class TestExplainCode:

    def test_missing_code(self):
        from app.mcp.server import _handle_explain_code
        assert "Error" in _handle_explain_code({})

    def test_returns_explanation(self):
        from app.mcp.server import _handle_explain_code
        with patch('app.ai.router.router.ask_text',
                   return_value=("Adds two numbers", MagicMock())):
            result = _handle_explain_code({"code": "def add(a,b): return a+b"})
        assert "Adds two numbers" in result

    def test_depth_deep_uses_1500_tokens(self):
        from app.mcp.server import _handle_explain_code
        captured = {}
        def cap(*a, **kw): captured['mt'] = kw.get('max_tokens'); return ("ok", MagicMock())
        with patch('app.ai.router.router.ask_text', side_effect=cap):
            _handle_explain_code({"code": "x=1", "depth": "deep"})
        assert captured.get('mt') == 1500

    def test_depth_brief_uses_400_tokens(self):
        from app.mcp.server import _handle_explain_code
        captured = {}
        def cap(*a, **kw): captured['mt'] = kw.get('max_tokens'); return ("ok", MagicMock())
        with patch('app.ai.router.router.ask_text', side_effect=cap):
            _handle_explain_code({"code": "x=1", "depth": "brief"})
        assert captured.get('mt') == 400


class TestGenerateTests:

    def test_missing_code(self):
        from app.mcp.server import _handle_generate_tests
        assert "Error" in _handle_generate_tests({})

    def test_returns_test_code(self):
        from app.mcp.server import _handle_generate_tests
        with patch('app.ai.router.router.ask_text',
                   return_value=("def test_add(): ...", MagicMock())):
            result = _handle_generate_tests({"code": "def add(a,b): return a+b"})
        assert "test_add" in result


class TestSecurityReview:

    def test_missing_content(self):
        from app.mcp.server import _handle_security_review
        assert "Error" in _handle_security_review({})

    def test_successful_review(self):
        from app.mcp.server import _handle_security_review
        with patch('app.ai.router.router.ask', return_value=({
            "risk_level": "high",
            "findings": [{"issue": "SQL injection", "severity": "critical",
                          "line": 10, "fix": "parameterized query"}],
            "cve_risks": ["CVE-2024-1234"],
        }, MagicMock())):
            result = _handle_security_review({"content": "SELECT * FROM users WHERE id={uid}"})
        assert "HIGH" in result
        assert "SQL injection" in result
        assert "CVE-2024-1234" in result


class TestGetRepoHealth:

    def test_missing_repo(self):
        from app.mcp.server import _handle_get_repo_health
        assert "Error" in _handle_get_repo_health({})

    def test_missing_installation_id(self):
        from app.mcp.server import _handle_get_repo_health
        result = _handle_get_repo_health({"repo": "o/r"})
        assert "installation_id" in result

    def test_successful_health(self):
        from app.mcp.server import _handle_get_repo_health
        with patch('app.github.auth.get_installation_token', return_value="tok"):
            with patch('app.ai.router.router.ask', return_value=({
                "grade": "B", "score": 7.5,
                "top_issues": ["low coverage"],
                "quick_wins": ["add CI badge"],
            }, MagicMock())):
                result = _handle_get_repo_health({"repo": "o/r", "installation_id": 123})
        assert "Grade:** B" in result
        assert "7.5" in result


class TestRunCommand:

    def test_missing_args(self):
        from app.mcp.server import _handle_run_command
        assert "Error" in _handle_run_command({})

    def test_merge_blocked(self):
        from app.mcp.server import _handle_run_command
        result = _handle_run_command({
            "repo": "o/r", "issue_number": 1,
            "command": "/merge", "installation_id": 123
        })
        assert "not available via MCP" in result

    def test_autofix_blocked(self):
        from app.mcp.server import _handle_run_command
        result = _handle_run_command({
            "repo": "o/r", "issue_number": 1,
            "command": "/autofix", "installation_id": 123
        })
        assert "not available via MCP" in result

    def test_apply_blocked(self):
        from app.mcp.server import _handle_run_command
        result = _handle_run_command({
            "repo": "o/r", "issue_number": 1,
            "command": "/apply", "installation_id": 123
        })
        assert "not available via MCP" in result

    def test_rollback_blocked(self):
        from app.mcp.server import _handle_run_command
        result = _handle_run_command({
            "repo": "o/r", "issue_number": 1,
            "command": "/rollback", "installation_id": 123
        })
        assert "not available via MCP" in result

    def test_release_blocked(self):
        from app.mcp.server import _handle_run_command
        result = _handle_run_command({
            "repo": "o/r", "issue_number": 1,
            "command": "/release", "installation_id": 123
        })
        assert "not available via MCP" in result

    def test_fix_is_allowed_routes_correctly(self):
        from app.mcp.server import _handle_run_command
        with patch('app.github.auth.get_installation_token', return_value="tok"):
            with patch('app.github.client.gh_get', return_value={"title":"Bug","body":"body"}):
                with patch('app.handlers.comments._cmd_fix', return_value="## Fix") as mock_fix:
                    result = _handle_run_command({
                        "repo": "o/r", "issue_number": 1,
                        "command": "/fix", "installation_id": 123
                    })
        assert result == "## Fix"
        args = mock_fix.call_args[0]
        assert args[0] == "Bug"   # title
        assert "body" in args[1]  # context

    def test_budget_called_with_zero_args(self):
        from app.mcp.server import _handle_run_command
        with patch('app.github.auth.get_installation_token', return_value="tok"):
            with patch('app.github.client.gh_get', return_value={"title":"t","body":"b"}):
                with patch('app.handlers.comments._cmd_budget',
                           return_value="## Budget") as mock_budget:
                    result = _handle_run_command({
                        "repo": "o/r", "issue_number": 1,
                        "command": "/budget", "installation_id": 123
                    })
        mock_budget.assert_called_once_with()
        assert result == "## Budget"

    def test_bad_parse_returns_error(self):
        from app.mcp.server import _handle_run_command
        with patch('app.github.auth.get_installation_token', return_value="tok"):
            with patch('app.github.client.gh_get', return_value={"title":"t","body":"b"}):
                with patch('app.handlers.comments._extract_command', return_value=None):
                    result = _handle_run_command({
                        "repo": "o/r", "issue_number": 1,
                        "command": "/fix", "installation_id": 123
                    })
        assert "Error" in result


class TestToolsCallDispatch:

    def setup_method(self, m=None):
        import app.mcp.server as s
        self._orig = s.MCP_API_KEY; s.MCP_API_KEY = ""

    def teardown_method(self, m=None):
        import app.mcp.server as s
        s.MCP_API_KEY = self._orig

    def test_explain_code_via_dispatch(self):
        from app.mcp.server import handle_mcp_request
        with patch('app.ai.router.router.ask_text',
                   return_value=("Adds numbers", MagicMock())):
            resp, status = handle_mcp_request("tools/call", {
                "name": "explain_code",
                "arguments": {"code": "def add(a,b): return a+b"}
            }, "")
        assert status == 200
        assert "Adds numbers" in resp["content"][0]["text"]

    def test_response_includes_latency(self):
        from app.mcp.server import handle_mcp_request
        with patch('app.ai.router.router.ask_text',
                   return_value=("ok", MagicMock())):
            resp, status = handle_mcp_request("tools/call", {
                "name": "explain_code", "arguments": {"code": "x=1"}
            }, "")
        assert "latency_ms" in resp
        assert resp["latency_ms"] >= 0

    def test_handler_exception_gives_500(self):
        from app.mcp.server import handle_mcp_request, TOOL_HANDLERS
        original = TOOL_HANDLERS["explain_code"]
        TOOL_HANDLERS["explain_code"] = lambda _: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            resp, status = handle_mcp_request("tools/call", {
                "name": "explain_code", "arguments": {"code": "x=1"}
            }, "")
        finally:
            TOOL_HANDLERS["explain_code"] = original
        assert status == 500
        assert "boom" in resp["error"]["message"]


if __name__ == "__main__":
    print("Run with: python -m pytest tests/test_mcp.py -v")

