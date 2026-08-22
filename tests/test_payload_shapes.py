"""
tests/test_payload_shapes.py

GitHub sends explicit nulls where a naive reader expects an object:

  user        the account was deleted
  head        the source fork or branch was deleted
  body        the comment was hidden or minimised
  patch       the file is binary, or the diff exceeded GitHub's size limit

Every one of these is documented and none is rare on a repository with any
history. The failure mode is what makes them worth a test file: the crash
happens inside a blanket `except Exception` — in server._run_handler, in
cmd_merge, in take_snapshot — so the event is dropped, or the user is told
"Merge failed", and nothing anywhere names the cause.

These tests drive the real entry points with the real payload shapes.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch



def _pr_payload(**over):
    payload = {
        "action": "opened",
        "pull_request": {
            "number": 7,
            "title": "add thing",
            "body": "",
            "user": {"login": "someone"},
            "head": {"ref": "feature", "sha": "abc"},
            "base": {"ref": "main"},
        },
        "repository": {"full_name": "o/r"},
        "installation": {"id": 123},
    }
    payload["pull_request"].update(over)
    return payload


def _issue_payload(**over):
    payload = {
        "action": "opened",
        "issue": {"number": 3, "title": "broken", "body": "x", "user": {"login": "someone"}},
        "repository": {"full_name": "o/r"},
        "installation": {"id": 123},
    }
    payload["issue"].update(over)
    return payload


class TestHandlersSurviveADeletedAccount:
    """These reads happen before the EventLogger exists, so the failure was
    invisible: server._run_handler caught the TypeError and the event was gone
    with a log line that named no cause."""

    def test_pull_request_handler_does_not_crash_on_a_null_user(self):
        from app.handlers.pull_request import handle

        with patch("app.github.auth.get_installation_token", side_effect=Exception("stop here")):
            handle(_pr_payload(user=None))  # must reach auth, not raise before it

    def test_issue_handler_does_not_crash_on_a_null_user(self):
        from app.handlers.issues import handle

        with patch("app.github.auth.get_installation_token", side_effect=Exception("stop here")):
            handle(_issue_payload(user=None))

    def test_a_ghosted_issue_is_still_triaged(self):
        """An empty author is not a reason to drop the issue. The bot-loop
        guard that follows checks for '[bot]' suffixes, which "" is not."""
        from app.handlers import issues

        with patch.object(issues, "get_installation_token", side_effect=Exception("reached")) as t:
            issues.handle(_issue_payload(user=None))
        assert t.called, "handler returned before auth — the ghosted issue was dropped"


class TestMergeSurvivesADeletedFork:
    def _merge(self, pr):
        from app.handlers.comments import publisher as P

        cfg = MagicMock()
        cfg.get.return_value = False
        with patch.object(P, "gh_get", return_value=pr):
            return P.cmd_merge("o/r", 7, {"pull_request": {}}, "tok", "someone", cfg)

    def test_a_null_head_says_why_instead_of_merge_failed(self):
        """The generic error is the one message that does not tell the
        maintainer anything actionable."""
        out = self._merge({"number": 7, "head": None, "base": {"ref": "main"}})
        assert "Cannot Merge" in out
        assert "deleted" in out.lower()
        assert "TypeError" not in out

    def test_a_head_with_no_sha_is_refused_the_same_way(self):
        out = self._merge({"number": 7, "head": {}, "base": {"ref": "main"}})
        assert "Cannot Merge" in out


class TestAutoMergeGuardrailNamesTheBlocker:
    def _check(self, reviews):
        from app.core.guardrails import check_pr_auto_merge

        cfg = MagicMock()
        cfg.get.side_effect = lambda *k, default=None: k == ("auto_merge", "require_no_blocking_reviews")
        pr = {"mergeable": True, "mergeable_state": "clean", "draft": False}
        return check_pr_auto_merge(pr, [], reviews, cfg)

    def test_a_change_request_from_a_deleted_account_still_blocks(self):
        """Raising here turns 'blocked by a review' into a generic failure —
        the merge is still refused, but for a reason the maintainer cannot act
        on, and the guardrail looks broken rather than correct."""
        result = self._check([{"state": "CHANGES_REQUESTED", "user": None}])
        assert result.passed is False
        assert "deleted account" in result.reason

    def test_a_normal_change_request_still_names_the_reviewer(self):
        result = self._check([{"state": "CHANGES_REQUESTED", "user": {"login": "alice"}}])
        assert result.passed is False
        assert "@alice" in result.reason


class TestTheSweepIsStructural:
    """A regression here would be a new bare subscript, not a changed one, so
    the guard has to be structural rather than a list of known sites."""

    NULLABLE = {"user", "head", "base", "patch"}

    def test_no_handler_bare_subscripts_a_nullable_payload_key(self):
        import ast
        import pathlib

        offenders = []
        for path in pathlib.Path("app").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Subscript):
                    continue
                sl = node.slice
                if not (isinstance(sl, ast.Constant) and sl.value in self.NULLABLE):
                    continue
                # A literal dict being built is a write, not a payload read.
                if isinstance(node.value, (ast.Name, ast.Subscript, ast.Attribute)):
                    offenders.append(f"{path}:{node.lineno} {ast.unparse(node)}")

        assert offenders == [], (
            "GitHub sends explicit nulls for these keys. Use `(x.get(k) or {})`:\n  "
            + "\n  ".join(offenders)
        )
