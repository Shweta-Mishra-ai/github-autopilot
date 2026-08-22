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
_req.adapters = MagicMock()
_req.adapters.HTTPAdapter = MagicMock
_req.Session = MagicMock
_req.exceptions = MagicMock()
_req.exceptions.RequestException = Exception
_req.exceptions.ConnectionError = ConnectionError
_req.exceptions.Timeout = TimeoutError
sys.modules.setdefault('requests', _req)
sys.modules.setdefault('requests.adapters', _req.adapters)
sys.modules.setdefault('requests.exceptions', _req.exceptions)
# Mock ONLY the deps that are genuinely missing. Blanket-mocking installed
# modules (this list used to include flask unconditionally) poisoned
# sys.modules for every later-collected test file and permanently skipped 21
# real-Flask tests. Real module available → use it; missing → mock it.
import importlib

for _m in ['structlog','redis','groq','google','google.generativeai',
           'flask_limiter','flask_limiter.util','apscheduler',
           'apscheduler.schedulers','apscheduler.schedulers.background',
           'sentence_transformers','qdrant_client','scipy','flask','flask.logging']:
    try:
        importlib.import_module(_m)
    except ImportError:
        sys.modules.setdefault(_m, MagicMock())

# Determine which module name GitHub used
_mcp_server_path = _ROOT / "app" / "mcp" / "mcp_server.py"
_mcp_server_alt  = _ROOT / "app" / "mcp" / "server.py"
_MCP_MODULE = "app.mcp.mcp_server" if _mcp_server_path.exists() else "app.mcp.server"

# v6: auth is fail-closed. A key MUST be configured; requests carry it as the token.
_TEST_KEY = "test-mcp-key"


def _import_mcp():
    """Import whichever mcp server module exists."""
    if _mcp_server_path.exists():
        import app.mcp.mcp_server as m
    else:
        import app.mcp.server as m
    return m


class TestMCPProtocol:

    def setup_method(self, m=None):
        import os
        self._env = patch.dict(os.environ, {"MCP_API_KEY": _TEST_KEY})
        self._env.start()

    def teardown_method(self, m=None):
        self._env.stop()

    def test_initialize(self):
        mod = _import_mcp()
        resp, status = mod.handle_mcp_request("initialize", {}, _TEST_KEY)
        assert status == 200
        assert resp["protocolVersion"] == "2024-11-05"
        assert resp["serverInfo"]["name"] == "github-autopilot"

    def test_tools_list_returns_the_whole_catalog(self):
        """Derived from MCP_TOOLS, not a literal: a hardcoded count fails on
        every tool added and tests nothing about the response beyond arithmetic.
        What matters is that tools/list advertises exactly the catalog."""
        mod = _import_mcp()
        resp, status = mod.handle_mcp_request("tools/list", {}, _TEST_KEY)
        assert status == 200
        assert len(resp["tools"]) == len(mod.MCP_TOOLS)
        assert {t["name"] for t in resp["tools"]} == {t["name"] for t in mod.MCP_TOOLS}

    def test_every_advertised_tool_has_a_handler(self):
        """A tool in the catalog with no handler is advertised but unusable."""
        mod = _import_mcp()
        for tool in mod.MCP_TOOLS:
            assert tool["name"] in mod.TOOL_HANDLERS, (
                f"{tool['name']} is advertised by tools/list but has no handler"
            )

    def test_every_handler_is_advertised(self):
        mod = _import_mcp()
        advertised = {t["name"] for t in mod.MCP_TOOLS}
        for name in mod.TOOL_HANDLERS:
            assert name in advertised, f"{name} has a handler but is not advertised"

    def test_each_tool_has_required_fields(self):
        mod = _import_mcp()
        for tool in mod.MCP_TOOLS:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool
            assert "required" in tool["inputSchema"]

    def test_unknown_method_returns_400(self):
        mod = _import_mcp()
        _, status = mod.handle_mcp_request("bad/method", {}, _TEST_KEY)
        assert status == 400

    def test_unknown_tool_returns_400_with_available(self):
        mod = _import_mcp()
        resp, status = mod.handle_mcp_request(
            "tools/call", {"name": "nonexistent", "arguments": {}}, _TEST_KEY
        )
        assert status == 400
        assert "available" in resp["error"]


class TestMCPAuth:

    def test_no_key_set_fails_closed(self):
        """v6: unset MCP_API_KEY must REJECT (503), never allow-all."""
        import os
        mod = _import_mcp()
        with patch.dict(os.environ, {"MCP_API_KEY": ""}):
            _, status = mod.handle_mcp_request("tools/list", {}, "")
        assert status == 503

    def test_wrong_token_gives_401(self):
        import os
        mod = _import_mcp()
        with patch.dict(os.environ, {"MCP_API_KEY": "correct"}):
            _, status = mod.handle_mcp_request("tools/list", {}, "wrong")
        assert status == 401

    def test_correct_token_gives_200(self):
        import os
        mod = _import_mcp()
        with patch.dict(os.environ, {"MCP_API_KEY": "correct"}):
            _, status = mod.handle_mcp_request("tools/list", {}, "correct")
        assert status == 200

    def test_empty_token_rejected_when_key_set(self):
        import os
        mod = _import_mcp()
        with patch.dict(os.environ, {"MCP_API_KEY": "correct"}):
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
        with patch('app.github.auth.get_installation_token', return_value="tok"), \
             patch('app.github.client.gh_get', return_value={"title": "PR"}), \
             patch('app.ai.router.router.ask', return_value=({
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
        with patch('app.github.auth.get_installation_token', return_value="tok"), \
             patch('app.github.client.gh_get', return_value={
                "title": "Bug", "body": "crashes"
            }), \
             patch('app.ai.router.router.ask', return_value=({
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
        def cap(*a, **kw):
            captured['mt'] = kw.get('max_tokens')
            return ("ok", MagicMock())
        with patch('app.ai.router.router.ask_text', side_effect=cap):
            mod._handle_explain_code({"code": "x=1", "depth": "deep"})
        assert captured.get('mt') == 1500

    def test_depth_brief_uses_400_tokens(self):
        mod = _import_mcp()
        captured = {}
        def cap(*a, **kw):
            captured['mt'] = kw.get('max_tokens')
            return ("ok", MagicMock())
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
        with patch('app.github.auth.get_installation_token', return_value="tok"), \
             patch('app.ai.router.router.ask', return_value=({
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
        with patch('app.github.auth.get_installation_token', return_value="tok"), \
             patch('app.github.client.gh_get', return_value={"title":"Bug","body":"body"}), \
             patch('app.handlers.comments._cmd_fix',
                        return_value="## Fix") as mock_fix:
                    result = mod._handle_run_command({
                        "repo": "o/r", "issue_number": 1,
                        "command": "/fix", "installation_id": 123
                    })
        assert result == "## Fix"
        assert mock_fix.call_args[0][0] == "Bug"

    def test_budget_called_with_zero_args(self):
        mod = _import_mcp()
        with patch('app.github.auth.get_installation_token', return_value="tok"), \
             patch('app.github.client.gh_get', return_value={"title":"t","body":"b"}), \
             patch('app.handlers.comments._cmd_budget',
                        return_value="## Budget") as mock_budget:
                    result = mod._handle_run_command({
                        "repo": "o/r", "issue_number": 1,
                        "command": "/budget", "installation_id": 123
                    })
        mock_budget.assert_called_once_with()
        assert result == "## Budget"

    def test_bad_parse_returns_error(self):
        mod = _import_mcp()
        with patch('app.github.auth.get_installation_token', return_value="tok"), \
             patch('app.github.client.gh_get', return_value={"title":"t","body":"b"}), \
             patch('app.handlers.comments._extract_command', return_value=None):
                    result = mod._handle_run_command({
                        "repo": "o/r", "issue_number": 1,
                        "command": "/fix", "installation_id": 123
                    })
        assert "Error" in result


class TestToolsCallDispatch:

    def setup_method(self, m=None):
        import os
        self._env = patch.dict(os.environ, {"MCP_API_KEY": _TEST_KEY})
        self._env.start()

    def teardown_method(self, m=None):
        self._env.stop()

    def test_explain_code_via_dispatch(self):
        mod = _import_mcp()
        with patch('app.ai.router.router.ask_text',
                   return_value=("Adds numbers", MagicMock())):
            resp, status = mod.handle_mcp_request("tools/call", {
                "name": "explain_code",
                "arguments": {"code": "def add(a,b): return a+b"}
            }, _TEST_KEY)
        assert status == 200
        assert "Adds numbers" in resp["content"][0]["text"]

    def test_response_includes_latency(self):
        mod = _import_mcp()
        with patch('app.ai.router.router.ask_text',
                   return_value=("ok", MagicMock())):
            resp, status = mod.handle_mcp_request("tools/call", {
                "name": "explain_code", "arguments": {"code": "x=1"}
            }, _TEST_KEY)
        assert "latency_ms" in resp
        assert resp["latency_ms"] >= 0

    def test_handler_exception_gives_500(self):
        mod = _import_mcp()
        original = mod.TOOL_HANDLERS["explain_code"]
        mod.TOOL_HANDLERS["explain_code"] = lambda _: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            resp, status = mod.handle_mcp_request("tools/call", {
                "name": "explain_code", "arguments": {"code": "x=1"}
            }, _TEST_KEY)
        finally:
            mod.TOOL_HANDLERS["explain_code"] = original
        assert status == 500
        assert "boom" in resp["error"]["message"]


class TestMCPNamedKeys:
    """V6.2: MCP_API_KEYS name:key pairs — per-client revocation + audit labels."""

    def _env(self, **vars):
        import os
        return patch.dict(os.environ, vars, clear=False)

    def test_named_key_authenticates(self):
        mod = _import_mcp()
        with self._env(MCP_API_KEY="", MCP_API_KEYS="laptop:tok-a,ci:tok-b"):
            resp, status = mod.handle_mcp_request("tools/list", {}, "tok-b")
        assert status == 200

    def test_wrong_token_rejected_401(self):
        mod = _import_mcp()
        with self._env(MCP_API_KEY="", MCP_API_KEYS="laptop:tok-a"):
            resp, status = mod.handle_mcp_request("tools/list", {}, "tok-WRONG")
        assert status == 401

    def test_legacy_and_named_coexist(self):
        mod = _import_mcp()
        with self._env(MCP_API_KEY="legacy-tok", MCP_API_KEYS="ci:tok-b"):
            assert mod.handle_mcp_request("tools/list", {}, "legacy-tok")[1] == 200
            assert mod.handle_mcp_request("tools/list", {}, "tok-b")[1] == 200

    def test_no_keys_at_all_fails_closed_503(self):
        mod = _import_mcp()
        with self._env(MCP_API_KEY="", MCP_API_KEYS=""):
            resp, status = mod.handle_mcp_request("tools/list", {}, "anything")
        assert status == 503

    def test_malformed_entry_skipped_not_fatal(self):
        """A typo'd entry must not break the valid one — and must never
        accidentally authenticate."""
        mod = _import_mcp()
        with self._env(MCP_API_KEY="", MCP_API_KEYS="brokenentry,ci:tok-b"):
            assert mod.handle_mcp_request("tools/list", {}, "tok-b")[1] == 200
            assert mod.handle_mcp_request("tools/list", {}, "brokenentry")[1] == 401

    def test_audit_metric_labeled_by_client(self):
        from app.core.metrics import metrics
        mod = _import_mcp()
        with self._env(MCP_API_KEY="", MCP_API_KEYS="ci:tok-b"):
            before = metrics.get("mcp.calls.ci")
            original = mod.TOOL_HANDLERS["explain_code"]
            mod.TOOL_HANDLERS["explain_code"] = lambda _: "ok"
            try:
                _, status = mod.handle_mcp_request(
                    "tools/call", {"name": "explain_code", "arguments": {"code": "x"}}, "tok-b"
                )
            finally:
                mod.TOOL_HANDLERS["explain_code"] = original
        assert status == 200
        assert metrics.get("mcp.calls.ci") == before + 1


if __name__ == "__main__":
    print("Run with: python -m pytest tests/test_mcp.py -v")


class TestRunCommandBindingsAreReal:
    """
    `_handle_run_command` maps 17 slash commands to lambdas over
    app.handlers.comments. A comment above that map says "Signatures verified
    against comments.py" — which was true when someone typed it, and is exactly
    the kind of claim that goes stale silently: every call site sits inside a
    blanket `except Exception`, so a renamed function or a changed signature
    turns into "Error: ..." returned to the IDE, with nothing failing in CI.

    The names are reached through backwards-compatible `_cmd_*` aliases, so
    hasattr() alone proves nothing useful either — the binding has to be
    checked against the real signature.
    """

    # command -> (attribute the map uses, positional args the lambda supplies)
    BINDINGS = {
        "/fix": ("_cmd_fix", ("title", "ctx")),
        "/explain": ("_cmd_explain", ("ctx",)),
        "/improve": ("_cmd_improve", ("ctx",)),
        "/refactor": ("_cmd_refactor", ("ctx",)),
        "/perf": ("_cmd_perf", ("ctx",)),
        "/gaps": ("_cmd_gaps", ("ctx",)),
        "/docs": ("_cmd_docs", ("ctx",)),
        "/test": ("_cmd_test", ("ctx",)),
        "/arch": ("_cmd_arch", ("repo", 1, {}, "tok")),
        "/impact": ("_cmd_impact", ("repo", 1, {}, "tok")),
        "/summarize": ("_cmd_summarize", ("repo", 1, "tok")),
        "/security": ("_cmd_security", ("repo", 1, {}, "tok")),
        "/changelog": ("_cmd_changelog", ("repo", "tok")),
        "/health": ("_cmd_health", ("repo", "tok")),
        "/version": ("_cmd_version", ("repo", "tok")),
        "/report": ("_cmd_report", ("repo",)),
        "/budget": ("_cmd_budget", ()),
    }

    def test_every_mapped_command_binds_against_its_real_signature(self):
        import inspect

        import app.handlers.comments as ch

        failures = []
        for cmd, (attr, args) in self.BINDINGS.items():
            fn = getattr(ch, attr, None)
            if fn is None:
                failures.append(f"{cmd}: {attr} does not exist")
                continue
            try:
                inspect.signature(fn).bind(*args)
            except TypeError as exc:
                failures.append(f"{cmd}: {attr}{inspect.signature(fn)} — {exc}")

        assert failures == [], "MCP run_command would fail at runtime for:\n  " + "\n  ".join(
            failures
        )

    def test_the_allowlist_and_the_handler_map_agree(self):
        """A command in ALLOWED with no handler answers 'allowed but not yet
        wired' — a dead end the tool advertises. One wired but not allowed is
        unreachable code."""
        import pathlib
        import re

        src = pathlib.Path("app/mcp/handlers.py").read_text(encoding="utf-8")
        allowed_block = src.split("ALLOWED = {", 1)[1].split("}", 1)[0]
        allowed = set(re.findall(r'"(/[a-z]+)"', allowed_block))

        assert allowed == set(self.BINDINGS), (
            f"only in ALLOWED: {sorted(allowed - set(self.BINDINGS))}; "
            f"only wired: {sorted(set(self.BINDINGS) - allowed)}"
        )

    def test_destructive_commands_are_not_reachable_over_mcp(self):
        """These write to GitHub. They require a comment on the issue so the
        action has an audit trail with a named author; an MCP key has neither."""
        import pathlib

        src = pathlib.Path("app/mcp/handlers.py").read_text(encoding="utf-8")
        allowed_block = src.split("ALLOWED = {", 1)[1].split("}", 1)[0]

        for destructive in ("/merge", "/autofix", "/apply", "/release", "/rollback", "/runtests"):
            assert f'"{destructive}"' not in allowed_block, f"{destructive} is reachable via MCP"

    def test_the_tool_description_lists_what_is_actually_allowed(self):
        """The description is what an IDE agent reads to decide what to call.
        A command listed there but refused by ALLOWED is a promise the tool
        breaks on use."""
        from app.mcp.tools import MCP_TOOLS

        described = next(t for t in MCP_TOOLS if t["name"] == "run_command")["description"]
        for cmd in self.BINDINGS:
            assert cmd in described, f"{cmd} is wired but not advertised"
