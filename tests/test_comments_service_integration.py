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
        assert "Not Run" in body["body"]
        assert "needs write access" in body["body"]

    def test_denial_header_does_not_assert_a_cause(self, common_mocks):
        """The header must not claim "requires maintainer access" — when the
        permission API itself failed, that framing points the maintainer at
        their own account instead of the App installation."""
        common_mocks["perm"].return_value = (
            False,
            "The permission check could not be completed",
        )
        with patch("app.handlers.comments.service._dispatch"):
            handle_comment_event(_payload(body="/merge"))

        _, _, body = common_mocks["post"].call_args[0]
        assert "requires maintainer access" not in body["body"]
        assert "could not be completed" in body["body"]


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
    def test_empty_response_is_answered_rather_than_swallowed(self, common_mocks):
        """
        This assertion was inverted deliberately. It used to require that an
        empty handler result posted *nothing*.

        That is the wrong contract for this branch. It is reached only after
        the comment carried a real command, the author was permitted to run
        it, and the command was enabled — so the user is waiting for an
        answer. Posting nothing is indistinguishable from the deployment being
        down, the webhook never arriving, or the command not existing, and all
        three send the reader somewhere useless.

        Silence is correct elsewhere in this codebase — a re-pushed PR with a
        clean review says nothing, because nobody asked it a question. Here
        somebody did.
        """
        with patch("app.handlers.comments.service._dispatch", return_value=None):
            handle_comment_event(_payload())

        common_mocks["post"].assert_called_once()
        _, _, body = common_mocks["post"].call_args[0]
        assert "No Output" in body["body"]
        assert "/fix" in body["body"]

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
            ("/security", "app.handlers.comments.security.cmd_security"),
            ("/secfull", "app.handlers.comments.security.cmd_secfull"),
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


class TestNoCommandEverAnswersWithSilence:
    """
    A user typed a documented command they were permitted to run. Every path
    out of handle_comment_event() must end in a comment.

    Silence is the worst available answer: it is indistinguishable from the
    service being down, from the webhook never arriving, and from the command
    not existing — so the reader retries, then files an issue, then stops
    using the bot. The dispatcher used to `return` on a log line when a
    handler produced nothing.
    """

    @staticmethod
    def _payload(body="/explain this"):
        return {
            "action": "created",
            "comment": {"body": body},
            "issue": {"number": 7, "title": "t", "body": "b"},
            "repository": {"full_name": "o/r"},
            "installation": {"id": 1},
            "sender": {"login": "someone"},
        }

    def test_an_empty_handler_result_still_posts_a_comment(self, monkeypatch):
        from app.handlers.comments import service

        posted = []
        monkeypatch.setattr(service, "get_installation_token", lambda _id: "tok")
        monkeypatch.setattr(service, "load_config", lambda *a, **k: _PermissiveConfig())
        monkeypatch.setattr(service, "check_user_rate_limit", lambda *a: True)
        monkeypatch.setattr(service, "check_command_permission", lambda *a: (True, ""))
        monkeypatch.setattr(service, "augment_with_memory", lambda ctx, *a: ctx)
        monkeypatch.setattr(service, "_dispatch", lambda **kw: "")
        monkeypatch.setattr(
            service, "gh_post", lambda path, token, body: posted.append(body["body"])
        )

        service.handle_comment_event(self._payload())

        assert posted, "a command that produced nothing posted no comment at all"
        assert "/explain" in posted[0]
        assert "No Output" in posted[0]

    def test_the_reply_tells_the_reader_it_is_not_their_fault(self):
        from app.handlers.comments.dispatcher import empty_response_comment

        text = empty_response_comment("/fix")
        assert "/fix" in text
        assert "not something you did wrong" in text
        assert "/health" in text  # points at where the cause would show

    def test_every_dispatcher_arm_returns_something(self):
        """The guard above is the safety net. This is the actual contract:
        no `case` in the dispatcher may fall through to an implicit None."""
        import re
        from pathlib import Path

        src = Path("app/handlers/comments/service.py").read_text(encoding="utf-8")
        body = src.split("match cmd:", 1)[1].split("except Exception", 1)[0]
        arms = re.findall(r'case "(/[a-z]+)":\n((?:.*\n)*?)(?=\s{12}case |\Z)', body)
        assert arms, "could not parse the dispatcher"
        for cmd, block in arms:
            assert "return " in block, f"{cmd} can fall through without returning"


class _PermissiveConfig:
    footer = ""

    def command_enabled(self, _cmd):
        return True

    def get(self, *a, **kw):
        return kw.get("default")
