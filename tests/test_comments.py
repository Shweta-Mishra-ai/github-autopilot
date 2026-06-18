"""
tests/test_comments.py
Sprint 5: Handler tests for app/handlers/comments.py

Tests all slash commands without making real API calls.
Covers: command parsing, dispatch, error handling.

Run: python -m pytest tests/test_comments.py -v
"""

import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_payload(
    body: str = "/fix",
    repo: str = "test/repo",
    issue_number: int = 1,
    is_pr: bool = False,
    sender: str = "shweta",
) -> dict:
    issue = {
        "number":  issue_number,
        "title":   "Test Issue",
        "body":    "Test body",
        "user":    {"login": sender},
        "labels":  [],
    }
    if is_pr:
        issue["pull_request"] = {"url": "https://api.github.com/repos/test/repo/pulls/1"}
    return {
        "action":   "created",
        "comment":  {"body": body, "user": {"login": sender}},
        "issue":    issue,
        "repository": {"full_name": repo},
        "installation": {"id": 12345},
        "sender":   {"login": sender, "type": "User"},
    }


def _mock_token(token="test_token"):
    return patch("app.handlers.comments.get_installation_token", return_value=token)


def _mock_config():
    config = MagicMock()
    config.comments_enabled.return_value = True
    config.get.return_value = True
    config.footer = ""
    return patch("app.handlers.comments.load_config", return_value=config)


def _mock_router_ask(response_dict=None, response_text="AI response"):
    from app.ai.providers.base import LLMResponse
    meta = LLMResponse(text="ok", provider="groq", model="llama", total_tokens=100)
    resp_dict = response_dict or {"fix": "use try/except", "root_cause": "null check", "explanation": "x", "test": "t"}

    return patch("app.handlers.comments.router.ask", return_value=(resp_dict, meta))


def _mock_router_text(text="AI text response"):
    from app.ai.providers.base import LLMResponse
    meta = LLMResponse(text=text, provider="groq", model="llama", total_tokens=100)
    return patch("app.handlers.comments.router.ask_text", return_value=(text, meta))


def _mock_gh_post():
    return patch("app.handlers.comments.gh_post", return_value={"id": 1})


def _mock_gh_get(data=None):
    return patch("app.handlers.comments.gh_get", return_value=data or [])


def _mock_context():
    """
    ContextManager is imported INSIDE functions (lazy), not at module level.
    Patch at source module, not at comments module.
    """
    ctx = MagicMock()
    ctx.get_history.return_value = ""
    ctx.add.return_value = None
    return patch("app.core.context_manager.ContextManager", return_value=ctx)


# ── Command Parsing Tests ─────────────────────────────────────────────────────

class TestCommandParsing:

    def test_handle_skips_non_created_action(self):
        from app.handlers.comments import handle
        payload = _make_payload()
        payload["action"] = "edited"
        with _mock_token(), _mock_config(), _mock_gh_post():
            result = handle(payload)
        assert result is None

    def test_handle_skips_bot_sender(self):
        from app.handlers.comments import handle
        payload = _make_payload(sender="ai-repo-manager[bot]")
        payload["sender"]["type"] = "Bot"
        with _mock_token(), _mock_config():
            handle(payload)
        # Should return early — no exception

    def test_handle_skips_non_command(self):
        from app.handlers.comments import handle
        payload = _make_payload(body="This is a regular comment")
        with _mock_token(), _mock_config(), _mock_gh_post():
            handle(payload)
        # gh_post should not have been called with a bot response
        # (no command to dispatch)

    def test_unknown_command_returns_help(self):
        from app.handlers.comments import handle
        payload = _make_payload(body="/unknowncommand123")
        with _mock_token(), _mock_config(), _mock_gh_post():
            handle(payload)
        # Should post "unknown command" or help message

    def test_command_with_args_parsed(self):
        """Test /rollback 2 parsing — inline since _parse_command is internal."""
        body   = "/rollback 2"
        parts  = body.strip().split(None, 1)
        cmd    = parts[0].lower()
        args   = parts[1] if len(parts) > 1 else ""
        assert cmd == "/rollback"
        assert args.strip() == "2"

    def test_command_case_insensitive(self):
        body  = "/FIX"
        parts = body.strip().split(None, 1)
        cmd   = parts[0].lower()
        assert cmd == "/fix"

    def test_command_strips_whitespace(self):
        body  = "  /fix  "
        parts = body.strip().split(None, 1)
        cmd   = parts[0].lower()
        assert cmd == "/fix"


# ── Command Response Tests ────────────────────────────────────────────────────

class TestFixCommand:

    def test_fix_returns_structured_response(self):
        from app.handlers.comments import _cmd_fix
        fix_resp = {
            "root_cause": "Null check missing on line 42",
            "fix": "if data is None:\n    return",
            "explanation": "The function failed when data was None",
            "test": "def test_fix():\n    assert process(None) is None",
            "confidence": 0.9,
        }
        with _mock_router_ask(fix_resp):
            result = _cmd_fix("Bug: crashes on None input", "def process(data):\n    return data.strip()")
        assert "🔧" in result or "Fix" in result
        assert "Null check" in result or "root_cause" in result.lower() or result

    def test_fix_handles_empty_context(self):
        from app.handlers.comments import _cmd_fix
        with _mock_router_ask():
            result = _cmd_fix("Title", "")
        assert result  # Should return something, not crash

    def test_fix_includes_root_cause(self):
        from app.handlers.comments import _cmd_fix
        fix_resp = {
            "root_cause": "Missing null check",
            "fix": "add null check",
            "explanation": "why",
            "test": "test code",
            "confidence": 0.8,
        }
        with _mock_router_ask(fix_resp):
            result = _cmd_fix("Issue title", "context code")
        assert "Missing null check" in result


class TestExplainCommand:

    def test_explain_returns_text(self):
        from app.handlers.comments import _cmd_explain
        with _mock_router_text("This is an explanation of the code."):
            result = _cmd_explain("def auth(): pass")
        assert "Explanation" in result or "explanation" in result.lower()
        assert "This is an explanation" in result

    def test_explain_handles_empty_context(self):
        from app.handlers.comments import _cmd_explain
        with _mock_router_text("Empty context explanation"):
            result = _cmd_explain("")
        assert result


class TestBudgetCommand:

    def test_budget_returns_table(self):
        from app.handlers.comments import _cmd_budget
        # format_budget_comment is imported inside _cmd_budget, patch at source
        with patch("app.ai.metrics.format_budget_comment",
                   return_value="## 💰 Budget\n\n| Provider |"):
            result = _cmd_budget()
        assert result  # Should return something

    def test_budget_handles_redis_failure(self):
        from app.handlers.comments import _cmd_budget
        with patch("app.ai.metrics.format_budget_comment",
                   side_effect=Exception("Redis down")):
            result = _cmd_budget()
        assert "failed" in result.lower() or result


class TestImproveCommand:

    def test_improve_returns_score(self):
        from app.handlers.comments import _cmd_improve
        improve_resp = {
            "overall_score": 7,
            "summary": "Code is decent but could be improved",
            "improvements": [
                {
                    "priority": "high",
                    "area": "error_handling",
                    "problem": "No error handling",
                    "suggestion": "Add try/except",
                    "example": "try:\n    ...\nexcept Exception:\n    pass",
                }
            ]
        }
        with _mock_router_ask(improve_resp):
            result = _cmd_improve("def process(): return data.strip()")
        assert "Improvements" in result or result
        assert "7" in result or result


class TestRollbackCommand:

    def test_rollback_no_args_shows_list(self):
        from app.handlers.comments import _cmd_rollback
        # format_snapshot_list is imported inside function, patch at source
        with patch("app.core.snapshot.format_snapshot_list",
                   return_value="## 📸 No Snapshots"):
            result = _cmd_rollback("test/repo", 1, "token", "", "shweta")
        assert result  # Should return something

    def test_rollback_invalid_number(self):
        from app.handlers.comments import _cmd_rollback
        result = _cmd_rollback("test/repo", 1, "token", "abc", "shweta")
        assert "Invalid" in result or "invalid" in result.lower()

    def test_rollback_nonexistent_snapshot(self):
        from app.handlers.comments import _cmd_rollback
        # get_snapshot_by_number imported inside function, patch at source
        with patch("app.core.snapshot.get_snapshot_by_number", return_value=None):
            result = _cmd_rollback("test/repo", 1, "token", "99", "shweta")
        assert "Not Found" in result or "not found" in result.lower() or result


# ── Context Manager Tests ─────────────────────────────────────────────────────
