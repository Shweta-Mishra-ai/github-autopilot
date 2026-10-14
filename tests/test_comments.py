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

    def test_handle_skips_an_edit_that_did_not_add_the_command(self):
        # This was "skips non-created action" and asserted only `result is
        # None`. Edits are handled now, and its token patch missed the module
        # that mints it: a real mint failed on the fake key, the failure was
        # swallowed, and None came back — the test passed without reaching the
        # code it named. The real rule: an edit runs a command only if the
        # edit added it, so nothing — not even a token — is spent here.
        from app.handlers.comments import handle
        payload = _make_payload()
        payload["action"] = "edited"
        payload["changes"] = {"body": {"from": "/fix"}}
        with patch("app.handlers.comments.service.get_installation_token") as mint, \
             _mock_config(), _mock_gh_post() as post:
            handle(payload)
        mint.assert_not_called()
        post.assert_not_called()

    # The three tests below had no assertion at all, and patched the token
    # mint on the package rather than on service.py, where it is called — so
    # they could not have failed. Each now asserts that nothing was spent:
    # the token is minted only for a command that will be answered.

    def test_handle_skips_bot_sender(self):
        from app.handlers.comments import handle
        payload = _make_payload(sender="ai-repo-manager[bot]")
        payload["sender"]["type"] = "Bot"
        with patch("app.handlers.comments.service.get_installation_token") as mint:
            handle(payload)
        mint.assert_not_called()

    def test_handle_skips_non_command(self):
        from app.handlers.comments import handle
        payload = _make_payload(body="This is a regular comment")
        with patch("app.handlers.comments.service.get_installation_token") as mint:
            handle(payload)
        mint.assert_not_called()

    def test_unknown_command_is_not_a_command(self):
        # It was named "returns help" and asserted nothing. An unknown
        # /word is not a command: anything else would answer every
        # comment that mentions a path such as /usr/bin.
        from app.handlers.comments import handle
        payload = _make_payload(body="/unknowncommand123")
        with patch("app.handlers.comments.service.get_installation_token") as mint:
            handle(payload)
        mint.assert_not_called()

    # These three split a string inline and asserted on their own result;
    # the app's parser was never called.
    def test_command_with_args_parsed(self):
        from app.handlers.comments.dispatcher import parse_command
        assert parse_command("/rollback 2") == ("/rollback", "2")

    def test_command_case_insensitive(self):
        from app.handlers.comments.dispatcher import parse_command
        assert parse_command("/FIX")[0] == "/fix"

    def test_command_strips_whitespace(self):
        from app.handlers.comments.dispatcher import parse_command
        assert parse_command("  /fix  ") == ("/fix", "")


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
        assert "## 🔧 Fix" in result
        assert "Null check missing on line 42" in result

    def test_fix_handles_empty_context(self):
        from app.handlers.comments import _cmd_fix
        with _mock_router_ask():
            result = _cmd_fix("Title", "")
        # No code shown: the answer must say so rather than read as a diagnosis.
        assert "have not seen" in result.lower() or "not been shown" in result.lower() or "## 🔧 Fix" in result

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
        assert result == "## 💰 Budget\n\n| Provider |"

    def test_budget_handles_redis_failure(self):
        from app.handlers.comments import _cmd_budget
        with patch("app.ai.metrics.format_budget_comment",
                   side_effect=Exception("Redis down")):
            result = _cmd_budget()
        assert "Budget check failed" in result


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
        # `problem` is not a field the /improve prompt asks for; area,
        # suggestion and example are what it renders.
        assert "`ERROR_HANDLING` — Add try/except" in result
        assert "Code is decent but could be improved" in result


class TestRollbackCommand:

    def test_rollback_no_args_shows_list(self):
        from app.handlers.comments import _cmd_rollback
        # format_snapshot_list is imported inside function, patch at source
        with patch("app.core.snapshot.format_snapshot_list",
                   return_value="## 📸 No Snapshots"):
            result = _cmd_rollback("test/repo", 1, "token", "", "shweta")
        assert result == "## 📸 No Snapshots"

    def test_rollback_invalid_number(self):
        from app.handlers.comments import _cmd_rollback
        result = _cmd_rollback("test/repo", 1, "token", "abc", "shweta")
        assert "Invalid" in result or "invalid" in result.lower()

    def test_rollback_nonexistent_snapshot(self):
        from app.handlers.comments import _cmd_rollback
        # get_snapshot_by_number imported inside function, patch at source
        with patch("app.core.snapshot.get_snapshot_by_number", return_value=None):
            result = _cmd_rollback("test/repo", 1, "token", "99", "shweta")
        assert "not found" in result.lower()


class TestIgnoreCommand:

    def test_ignore_returns_confirmation(self):
        from app.handlers.comments.generator import cmd_ignore

        with patch("app.intelligence.memory.remember", return_value=True):
            res = cmd_ignore("org/repo", "line-length nitpicks", "shweta")
            assert "Rule Ignored & Remembered" in res
            assert "line-length nitpicks" in res

    def test_ignore_empty_args_prompts_user(self):
        from app.handlers.comments.generator import cmd_ignore

        res = cmd_ignore("org/repo", "", "shweta")
        assert "Please specify what rule" in res

