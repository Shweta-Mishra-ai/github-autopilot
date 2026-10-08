"""
When a comment runs a command, and what the command is shown.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.handlers.comments.dispatcher import (
    command_repeated_by_edit,
    extract_command,
    pr_context,
)


class TestQuotesAndCodeAreNotInstructions:
    def test_quote_replying_the_bots_own_report_runs_nothing(self):
        body = (
            "> 💡 Use `/gaps` for a detailed analysis, or `/test` to generate the missing tests.\n\n"
            "Thanks, will look at this tomorrow."
        )
        assert extract_command(body) is None

    @pytest.mark.parametrize(
        "body",
        [
            "Logs:\n```\n$ bot /release\nerror\n```",
            "the `/merge` command is broken",
            "> /rollback 3 confirm",
            "~~~\n/autofix\n~~~",
        ],
    )
    def test_commands_inside_quotes_or_code_are_ignored(self, body):
        assert extract_command(body) is None

    def test_a_real_command_beside_a_quote_still_runs(self):
        assert extract_command("> earlier: /merge\n\n/fix the crash") == "/fix"

    def test_an_unclosed_fence_hides_the_rest(self):
        assert extract_command("```\n/release") is None


class TestEditsDoNotRepeatCommands:
    def _edit(self, old, new="/release"):
        return {"action": "edited", "changes": {"body": {"from": old}}, "comment": {"body": new}}

    def test_editing_a_comment_that_already_held_the_command_runs_nothing(self):
        assert command_repeated_by_edit(self._edit("/release v2"), "/release")

    def test_an_edit_that_adds_the_command_runs_it(self):
        assert not command_repeated_by_edit(self._edit("/relese"), "/release")

    def test_a_new_comment_is_never_a_repeat(self):
        assert not command_repeated_by_edit({"action": "created"}, "/release")

    def test_service_skips_a_repeated_command_before_any_api_call(self):
        from app.handlers.comments import service

        payload = {
            "action": "edited",
            "changes": {"body": {"from": "/release"}},
            "comment": {"body": "/release (typo fixed)"},
            "issue": {"number": 1},
            "repository": {"full_name": "o/r"},
            "installation": {"id": 1},
            "sender": {"login": "maintainer"},
        }
        with patch.object(service, "get_installation_token") as tok:
            service.handle_comment_event(payload)
        tok.assert_not_called()


class TestPrCommandsSeeTheDiff:
    def test_pr_context_carries_the_code(self):
        files = [{"filename": "app/db.py", "patch": "@@ -1 +1 @@\n+q = 'SELECT ' + uid"}]
        with patch("app.github.helpers.pr_files", return_value=files):
            ctx = pr_context("o/r", 3, {"title": "t", "body": "b"}, "tok", MagicMock())
        assert "q = 'SELECT ' + uid" in ctx and "app/db.py" in ctx

    def test_the_gaps_prompt_receives_the_diff(self):
        from app.handlers.comments import service

        seen = {}

        def fake_gaps(context):
            seen["context"] = context
            return "ok"

        files = [{"filename": "app/db.py", "patch": "@@ -1 +1 @@\n+def lookup(uid): pass"}]
        cfg = MagicMock()
        cfg.command_enabled.return_value = True
        payload = {
            "action": "created",
            "comment": {"body": "/gaps"},
            "issue": {"number": 3, "title": "t", "body": "b", "pull_request": {}},
            "repository": {"full_name": "o/r"},
            "installation": {"id": 1},
            "sender": {"login": "maintainer"},
        }
        with (
            patch.object(service, "get_installation_token", return_value="tok"),
            patch.object(service, "load_config", return_value=cfg),
            patch.object(service, "check_user_rate_limit", return_value=True),
            patch.object(service, "check_command_permission", return_value=(True, "")),
            patch("app.github.helpers.pr_files", return_value=files),
            patch("app.handlers.comments.generator.cmd_gaps", side_effect=fake_gaps),
            patch.object(service, "_post_comment"),
        ):
            service.handle_comment_event(payload)
        assert "def lookup(uid)" in seen["context"]


class TestGeneratedCodeIsNotCutSilently:
    def test_a_long_block_is_cut_on_a_line_and_says_so(self):
        from app.handlers.comments.generator import _clip

        code = "\n".join(f"assert f({i}) == {i}" for i in range(500))
        out = _clip(code, 300)
        assert out.endswith("more characters)")
        assert all(line.startswith("assert f(") for line in out.splitlines()[:-1])

    def test_a_short_block_is_untouched(self):
        from app.handlers.comments.generator import _clip

        assert _clip("x = 1", 300) == "x = 1"
