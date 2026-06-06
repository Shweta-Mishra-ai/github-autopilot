"""
tests/test_autofix.py
Sprint 6: Tests for app/handlers/autofix.py
"""
import sys
import os
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _mock_router(resp=None):
    from app.ai.providers.base import LLMResponse
    meta = LLMResponse(text="ok", provider="groq", model="llama", total_tokens=50)
    r = resp or {"target_file": "app/auth.py", "pr_title": "fix null check",
                 "commit_message": "fix: null check", "problem": "null crash",
                 "fix_description": "add null check", "explanation": "prevents crash",
                 "patch": "if x is None: return", "confidence": 0.9}
    return patch("app.handlers.autofix.router.ask", return_value=(r, meta))


class TestIsAllowed:
    def test_py_file_allowed(self):
        from app.handlers.autofix import _is_allowed
        assert _is_allowed("app/handlers/comments.py") is True

    def test_env_blocked(self):
        from app.handlers.autofix import _is_allowed
        assert _is_allowed(".env") is False

    def test_server_blocked(self):
        from app.handlers.autofix import _is_allowed
        assert _is_allowed("server.py") is False

    def test_auth_blocked(self):
        from app.handlers.autofix import _is_allowed
        assert _is_allowed("app/github/auth.py") is False

    def test_ci_workflow_blocked(self):
        """CI workflow files are blocked to prevent pipeline injection."""
        from app.handlers.autofix import _is_allowed
        assert _is_allowed(".github/workflows/ci.yml") is False

    def test_non_workflow_yaml_allowed(self):
        """Non-workflow YAML files (e.g. config) are still allowed."""
        from app.handlers.autofix import _is_allowed
        assert _is_allowed("config/settings.yml") is True

    def test_empty_path_blocked(self):
        from app.handlers.autofix import _is_allowed
        assert _is_allowed("") is False

    def test_no_extension_blocked(self):
        from app.handlers.autofix import _is_allowed
        assert _is_allowed("Makefile") is False


class TestBuildPrBody:
    def test_contains_issue_number(self):
        from app.handlers.autofix import _build_pr_body
        body = _build_pr_body(
            {"problem": "crash", "fix_description": "add check",
             "explanation": "prevents it", "confidence": 0.9},
            42, "Test crash"
        )
        assert "#42" in body

    def test_contains_closes(self):
        from app.handlers.autofix import _build_pr_body
        body = _build_pr_body({}, 7, "Bug")
        assert "Closes #7" in body

    def test_confidence_shown(self):
        from app.handlers.autofix import _build_pr_body
        body = _build_pr_body({"confidence": 0.85}, 1, "Bug")
        assert "85%" in body


class TestGenerateFixPlan:
    def test_low_confidence_returns_none(self):
        from app.handlers.autofix import _generate_fix_plan
        from app.ai.providers.base import LLMResponse
        meta = LLMResponse(text="", provider="groq", model="llama", total_tokens=10)
        with patch("app.handlers.autofix.router.ask",
                   return_value=({"confidence": 0.4}, meta)):
            result = _generate_fix_plan("title", "body", "")
        assert result is None

    def test_good_plan_returned(self):
        from app.handlers.autofix import _generate_fix_plan
        from app.ai.providers.base import LLMResponse
        meta = LLMResponse(text="", provider="groq", model="llama", total_tokens=10)
        plan = {"target_file": "app/foo.py", "confidence": 0.9,
                "patch": "fix here", "pr_title": "fix bug"}
        with patch("app.handlers.autofix.router.ask", return_value=(plan, meta)):
            result = _generate_fix_plan("Bug title", "body", "")
        assert result is not None
        assert result["confidence"] == 0.9

    def test_router_exception_returns_none(self):
        from app.handlers.autofix import _generate_fix_plan
        with patch("app.handlers.autofix.router.ask", side_effect=Exception("error")):
            result = _generate_fix_plan("title", "body", "")
        assert result is None


class TestRunAutofix:
    def _issue(self):
        return {"title": "Fix null crash", "body": "crashes when None passed"}

    def test_no_fix_plan_returns_failed(self):
        from app.handlers.autofix import run_autofix
        from app.ai.providers.base import LLMResponse
        meta = LLMResponse(text="", provider="groq", model="llama", total_tokens=10)
        with patch("app.handlers.autofix.router.ask",
                   return_value=({"confidence": 0.1}, meta)):
            result = run_autofix("test/repo", 1, self._issue(), "token")
        assert "Failed" in result or "failed" in result.lower()

    def test_blocked_file_returns_skipped(self):
        from app.handlers.autofix import run_autofix
        from app.ai.providers.base import LLMResponse
        meta = LLMResponse(text="", provider="groq", model="llama", total_tokens=10)
        plan = {"target_file": "server.py", "confidence": 0.9,
                "patch": "x", "pr_title": "fix"}
        with patch("app.handlers.autofix.router.ask", return_value=(plan, meta)):
            result = run_autofix("test/repo", 1, self._issue(), "token")
        assert "Skipped" in result or "skipped" in result.lower() or "Blocked" in result

    def test_full_flow_success(self):
        from app.handlers.autofix import run_autofix
        from app.ai.providers.base import LLMResponse
        meta = LLMResponse(text="", provider="groq", model="llama", total_tokens=50)
        plan = {"target_file": "app/foo.py", "confidence": 0.9,
                "patch": "fix here", "pr_title": "fix null",
                "commit_message": "fix: null", "problem": "null",
                "fix_description": "add check", "explanation": "prevents crash"}
        fix_resp = {"fixed_content": "def foo():\n    if x is None: return\n    return x.strip()",
                    "changed_lines": 1}

        with patch("app.handlers.autofix.router.ask", side_effect=[(plan, meta), (fix_resp, meta)]):
            with patch("app.handlers.autofix.gh_get") as mock_get:
                mock_get.return_value = {
                    "content": __import__("base64").b64encode(b"def foo(): return x.strip()").decode(),
                    "sha": "abc123",
                    "default_branch": "main",
                    "object": {"sha": "abc123"},
                }
                with patch("app.handlers.autofix.gh_put", return_value={}):
                    with patch("app.handlers.autofix.gh_post") as mock_post:
                        mock_post.side_effect = [
                            {},  # create branch
                            {"number": 99, "html_url": "https://github.com/test/repo/pull/99"},
                        ]
                        result = run_autofix("test/repo", 1, self._issue(), "token")

        # V4.2+: autofix posts diff + confirmation, does NOT auto-create PR
        assert ("diff" in result.lower() or "Diff" in result
                or "/apply" in result
                or "Ready" in result)
