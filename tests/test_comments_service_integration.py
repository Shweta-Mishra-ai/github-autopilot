"""
tests/test_comments_service_integration.py — Integration tests for
app/handlers/comments/service.py::handle_comment_event().

WHY THIS FILE EXISTS
  Existing comment tests exercise individual command functions (generator.py,
  reviewer.py, ...) directly, bypassing the orchestration layer. That leaves
  handle_comment_event()'s own control flow — rate limiting, authorization,
  memory augmentation, dispatch-error handling, providers-down substitution,
  and comment posting — largely untested (41% coverage at last measurement).
  These tests drive the real function end-to-end with its direct dependencies
  mocked at the module boundary, the same pattern test_comments.py already
  uses for the command-level tests.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.handlers.comments.service import handle_comment_event


def _payload(body="/fix", repo="test/repo", issue_number=1, sender="alice"):
    return {
        "action": "created",
        "comment": {"body": body, "user": {"login": sender}},
        "issue": {"number": issue_number, "title": "Bug title", "body": "Bug body", "labels": []},
        "repository": {"full_name": repo},
        "installation": {"id": 999},
        "sender": {"login": sender, "type": "User"},
    }


@pytest.fixture(autouse=True)
def common_mocks():
    """Auth + config succeed by default; individual tests override as needed."""
    with (
        patch("app.handlers.comments.service.get_installation_token", return_value="tok") as token,
        patch("app.handlers.comments.service.load_config", return_value=MagicMock()) as config,
        patch(
            "app.handlers.comments.service.check_user_rate_limit", return_value=True
        ) as rate_limit,
        patch(
            "app.handlers.comments.service.check_command_permission",
            return_value=(True, ""),
        ) as perm,
        patch("app.handlers.comments.service.gh_post", return_value={}) as post,
    ):
        yield {
            "token": token,
            "config": config,
            "rate_limit": rate_limit,
            "perm": perm,
            "post": post,
        }


class TestHappyPath:
    def test_dispatches_and_posts_response(self, common_mocks):
        with patch(
            "app.handlers.comments.service._dispatch", return_value="## Fix\n\nDo the thing."
        ) as dispatch:
            handle_comment_event(_payload())

        assert dispatch.called
        common_mocks["post"].assert_called_once()
        posted_path, _token, body = common_mocks["post"].call_args[0]
        assert posted_path == "/repos/test/repo/issues/1/comments"
        assert "Do the thing." in body["body"]
        assert "requested by @alice" in body["body"]

    def test_memory_is_augmented_into_dispatch_context(self, common_mocks, monkeypatch):
        """
        augment_with_memory() must run before _dispatch(), and its output —
        not the raw context — must be what dispatch receives.
        """
        monkeypatch.setattr(
            "app.handlers.comments.service.augment_with_memory",
            lambda context, repo, query: context + "\n\nMEMORY_MARKER",
        )
        with patch("app.handlers.comments.service._dispatch", return_value="ok") as dispatch:
            handle_comment_event(_payload())

        _, kwargs = dispatch.call_args
        assert "MEMORY_MARKER" in kwargs["context"]


class TestEarlyExits:
    def test_ignored_action_never_calls_dispatch(self, common_mocks):
        with patch("app.handlers.comments.service._dispatch") as dispatch:
            handle_comment_event({**_payload(), "action": "deleted"})
        dispatch.assert_not_called()
        common_mocks["post"].assert_not_called()

    def test_missing_required_field_returns_early(self, common_mocks):
        payload = _payload(repo="")  # repository.full_name empty -> fails the `all([...])` guard
        with patch("app.handlers.comments.service._dispatch") as dispatch:
            handle_comment_event(payload)
        dispatch.assert_not_called()

    def test_bot_author_is_skipped(self, common_mocks):
        with patch("app.handlers.comments.service._dispatch") as dispatch:
            handle_comment_event(_payload(sender="some-bot[bot]"))
        dispatch.assert_not_called()

    def test_no_command_in_body_returns_early(self, common_mocks):
        with patch("app.handlers.comments.service._dispatch") as dispatch:
            handle_comment_event(_payload(body="just chatting, no slash command here"))
        dispatch.assert_not_called()

    def test_auth_failure_returns_early(self, common_mocks):
        common_mocks["token"].side_effect = Exception("token fetch failed")
        with patch("app.handlers.comments.service._dispatch") as dispatch:
            handle_comment_event(_payload())
        dispatch.assert_not_called()
        common_mocks["post"].assert_not_called()


class TestRateLimitAndAuthorization:
    def test_rate_limited_user_gets_a_message_not_a_dispatch(self, common_mocks):
        common_mocks["rate_limit"].return_value = False
        with patch("app.handlers.comments.service._dispatch") as dispatch:
            handle_comment_event(_payload())

        dispatch.assert_not_called()
        common_mocks["post"].assert_called_once()
        _, _, body = common_mocks["post"].call_args[0]
        assert "Rate Limit" in body["body"]

    def test_permission_denied_gets_a_message_not_a_dispatch(self, common_mocks):
        common_mocks["perm"].return_value = (False, "needs write access")
        with patch("app.handlers.comments.service._dispatch") as dispatch:
            handle_comment_event(_payload(body="/merge"))

        dispatch.assert_not_called()
        common_mocks["post"].assert_called_once()
        _, _, body = common_mocks["post"].call_args[0]
        assert "Permission Denied" in body["body"]
        assert "needs write access" in body["body"]


class TestCommandArgs:
    def test_args_preserve_original_case(self, common_mocks):
        """Regression: args must NOT be lowercased — they were sliced from
        body.lower(), which mangled /notify messages and branch names."""
        with patch(
            "app.handlers.comments.service._dispatch", return_value="ok"
        ) as dispatch:
            handle_comment_event(_payload(body="/notify Deploy FAILED on Prod"))

        assert dispatch.called
        assert dispatch.call_args.kwargs["cmd_args"] == "Deploy FAILED on Prod"

    def test_args_empty_when_no_trailing_text(self, common_mocks):
        with patch(
            "app.handlers.comments.service._dispatch", return_value="ok"
        ) as dispatch:
            handle_comment_event(_payload(body="/fix"))

        assert dispatch.call_args.kwargs["cmd_args"] == ""


class TestDispatchOutcomes:
    def test_empty_response_is_not_posted(self, common_mocks):
        with patch("app.handlers.comments.service._dispatch", return_value=None):
            handle_comment_event(_payload())
        common_mocks["post"].assert_not_called()

    def test_providers_down_sentinel_becomes_degraded_message(self, common_mocks):
        sentinel = {"_providers_down": True, "_retry_in": 42}
        with patch("app.handlers.comments.service._dispatch", return_value=sentinel):
            handle_comment_event(_payload())

        common_mocks["post"].assert_called_once()
        _, _, body = common_mocks["post"].call_args[0]
        assert "Temporarily Unavailable" in body["body"]

    def test_dispatch_exception_is_caught_and_formatted(self, common_mocks):
        """_dispatch() itself never raises (it catches internally) — but verify
        the real function's own try/except still holds if a handler misbehaves."""
        from app.handlers.comments.service import _dispatch

        with patch("app.handlers.comments.generator.cmd_fix", side_effect=RuntimeError("boom")):
            result = _dispatch(
                cmd="/fix",
                cmd_args="",
                context="ctx",
                repo="o/r",
                issue_number=1,
                issue={"title": "t"},
                token="tok",
                author="alice",
                config=MagicMock(),
                log_ctx=MagicMock(),
            )
        assert result is not None
        assert "/fix" in result

    def test_post_comment_failure_does_not_raise(self, common_mocks):
        from app.github.client import GitHubError

        common_mocks["post"].side_effect = GitHubError("posting failed", 500)
        with patch("app.handlers.comments.service._dispatch", return_value="some response"):
            handle_comment_event(_payload())  # must not raise


class TestDispatchRoutingTable:
    """
    Every command in the `match cmd:` table must route to its documented
    handler function. A typo'd `case` value here would silently make a command
    a no-op (falls through to `unknown_command`) — this guards the whole table
    in one pass instead of one test per command.
    """

    @pytest.mark.parametrize(
        "cmd,target",
        [
            ("/fix", "app.handlers.comments.generator.cmd_fix"),
            ("/explain", "app.handlers.comments.generator.cmd_explain"),
            ("/improve", "app.handlers.comments.generator.cmd_improve"),
            ("/test", "app.handlers.comments.generator.cmd_test"),
            ("/docs", "app.handlers.comments.generator.cmd_docs"),
            ("/refactor", "app.handlers.comments.generator.cmd_refactor"),
            ("/gaps", "app.handlers.comments.generator.cmd_gaps"),
            ("/perf", "app.handlers.comments.generator.cmd_perf"),
            ("/arch", "app.handlers.comments.generator.cmd_arch"),
            ("/health", "app.handlers.comments.reviewer.cmd_health"),
            ("/version", "app.handlers.comments.reviewer.cmd_version"),
            ("/summarize", "app.handlers.comments.reviewer.cmd_summarize"),
            ("/ci", "app.handlers.comments.reviewer.cmd_ci"),
            ("/budget", "app.handlers.comments.reviewer.cmd_budget"),
            ("/report", "app.handlers.comments.reviewer.cmd_report"),
            ("/impact", "app.handlers.comments.reviewer.cmd_impact"),
            ("/changelog", "app.handlers.comments.reviewer.cmd_changelog"),
            ("/merge", "app.handlers.comments.publisher.cmd_merge"),
            ("/apply", "app.handlers.comments.publisher.cmd_apply"),
            ("/rollback", "app.handlers.comments.publisher.cmd_rollback"),
            ("/release", "app.handlers.comments.publisher.cmd_release"),
            ("/runtests", "app.handlers.comments.publisher.cmd_runtests"),
            ("/notify", "app.handlers.comments.publisher.cmd_notify"),
            ("/security", "app.handlers.comments.publisher.cmd_security"),
            ("/secfull", "app.handlers.comments.publisher.cmd_secfull"),
        ],
    )
    def test_command_routes_to_expected_handler(self, cmd, target):
        from app.handlers.comments.service import _dispatch

        with patch(target, return_value=f"handled:{cmd}") as handler:
            result = _dispatch(
                cmd=cmd,
                cmd_args="",
                context="ctx",
                repo="o/r",
                issue_number=1,
                issue={"title": "t"},
                token="tok",
                author="alice",
                config=MagicMock(),
                log_ctx=MagicMock(),
            )
        assert handler.called
        assert result == f"handled:{cmd}"

    def test_autofix_routes_to_run_autofix(self):
        from app.handlers.comments.service import _dispatch

        with patch("app.handlers.autofix.run_autofix", return_value="autofix-done") as handler:
            result = _dispatch(
                cmd="/autofix",
                cmd_args=" some/file.py ",
                context="ctx",
                repo="o/r",
                issue_number=1,
                issue={"title": "t"},
                token="tok",
                author="alice",
                config=MagicMock(),
                log_ctx=MagicMock(),
            )
        assert handler.called
        assert handler.call_args[0][-1] == "some/file.py"  # cmd_args stripped
        assert result == "autofix-done"

    def test_unknown_command_returns_none(self):
        from app.handlers.comments.service import _dispatch

        result = _dispatch(
            cmd="/not-a-real-command",
            cmd_args="",
            context="ctx",
            repo="o/r",
            issue_number=1,
            issue={"title": "t"},
            token="tok",
            author="alice",
            config=MagicMock(),
            log_ctx=MagicMock(),
        )
        assert result is None
