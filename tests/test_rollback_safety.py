"""
tests/test_rollback_safety.py

/rollback is the one command that undoes other commands, and it is destructive.
These pin the safety properties that were not actually holding:

  1. The "abort if the safety snapshot fails" guard could never fire.
     take_snapshot() catches its own exceptions and returns None, so the
     try/except around it never saw anything — a failed safety snapshot was
     swallowed and the rollback proceeded with no way back.

  2. Actions were undone oldest-first. Undo is LIFO: with two recorded title
     edits on one PR (X->Y then Y->Z), oldest-first leaves the intermediate
     title instead of the original.

  3. An action type this version cannot undo was logged and skipped, and the
     user still read "Rollback Complete" for work that did not happen.

  4. take_snapshot() reads four GitHub responses with bare subscripting, and
     swallows the resulting KeyError as "no snapshot".
"""

from unittest.mock import MagicMock, patch

import pytest

from app.core import snapshot as S
from app.handlers.comments import publisher as P
from app.github.client import GitHubError


@pytest.fixture(autouse=True)
def _fresh_redis():
    from app.core.redis_client import reset_client

    reset_client()
    yield
    reset_client()


def _snapshot_with(actions):
    return {
        "id": "abc123",
        "timestamp": "2026-08-20T06:00:00+00:00",
        "trigger": "manual",
        "bot_actions": actions,
    }


class TestSafetySnapshotActuallyGuards:
    def test_rollback_aborts_when_the_safety_snapshot_fails(self):
        """take_snapshot returns None on failure rather than raising."""
        with (
            patch.object(P, "gh_put") as put,
            patch("app.core.snapshot.take_snapshot", return_value=None),
            patch(
                "app.core.snapshot.get_snapshot_by_number",
                return_value=_snapshot_with([{"type": "create_issue", "number": 7}]),
            ),
        ):
            out = P.cmd_rollback("o/r", 1, "tok", "1 confirm", "dev")

        assert "Rollback Aborted" in out
        put.assert_not_called(), "nothing may be undone without a way back"

    def test_rollback_aborts_when_take_snapshot_raises(self):
        with (
            patch.object(P, "gh_put") as put,
            patch("app.core.snapshot.take_snapshot", side_effect=RuntimeError("redis")),
            patch(
                "app.core.snapshot.get_snapshot_by_number",
                return_value=_snapshot_with([{"type": "create_issue", "number": 7}]),
            ),
        ):
            out = P.cmd_rollback("o/r", 1, "tok", "1 confirm", "dev")

        assert "Rollback Aborted" in out
        put.assert_not_called()

    def test_rollback_proceeds_when_the_safety_snapshot_succeeds(self):
        with (
            patch.object(P, "gh_put") as put,
            patch("app.core.snapshot.take_snapshot", return_value="safe1"),
            patch(
                "app.core.snapshot.get_snapshot_by_number",
                return_value=_snapshot_with([{"type": "create_issue", "number": 7}]),
            ),
        ):
            out = P.cmd_rollback("o/r", 1, "tok", "1 confirm", "dev")

        assert "Rollback Complete" in out
        put.assert_called_once()


class TestUndoOrdering:
    def test_repeated_title_edits_restore_the_original(self):
        """bot_actions is newest-first; undo must apply it in that order."""
        actions = [  # newest first, as _get_bot_actions returns them
            {"type": "edit_pr_title", "number": 5, "old_title": "second"},
            {"type": "edit_pr_title", "number": 5, "old_title": "original"},
        ]
        with (
            patch.object(P, "gh_put") as put,
            patch("app.core.snapshot.take_snapshot", return_value="safe1"),
            patch(
                "app.core.snapshot.get_snapshot_by_number",
                return_value=_snapshot_with(actions),
            ),
        ):
            P.cmd_rollback("o/r", 1, "tok", "1 confirm", "dev")

        applied = [c.args[2]["title"] for c in put.call_args_list]
        assert applied == ["second", "original"]
        assert applied[-1] == "original", (
            "the last write decides the final title; undoing oldest-first "
            "leaves the intermediate value"
        )

    def test_recorded_actions_come_back_newest_first(self):
        from app.core.redis_client import get_redis

        for title in ["first", "second", "third"]:
            S.record_bot_action("o/r", "s1", {"type": "edit_pr_title", "old_title": title})
        actions = S._get_bot_actions(get_redis(), "o/r", "s1")
        assert [a["old_title"] for a in actions] == ["third", "second", "first"]


class TestUnrecoverableActionsAreReported:
    def test_unknown_action_type_is_reported_as_failed(self):
        actions = [{"type": "deleted_branch", "number": 9}]
        with (
            patch.object(P, "gh_put"),
            patch("app.core.snapshot.take_snapshot", return_value="safe1"),
            patch(
                "app.core.snapshot.get_snapshot_by_number",
                return_value=_snapshot_with(actions),
            ),
        ):
            out = P.cmd_rollback("o/r", 1, "tok", "1 confirm", "dev")

        assert "Failed" in out
        assert "deleted_branch" in out, "silently skipping reported success"

    def test_known_action_without_a_number_is_reported(self):
        actions = [{"type": "create_issue"}]  # no number
        with (
            patch.object(P, "gh_put") as put,
            patch("app.core.snapshot.take_snapshot", return_value="safe1"),
            patch(
                "app.core.snapshot.get_snapshot_by_number",
                return_value=_snapshot_with(actions),
            ),
        ):
            out = P.cmd_rollback("o/r", 1, "tok", "1 confirm", "dev")

        put.assert_not_called()
        assert "no issue/PR number recorded" in out

    def test_api_failure_on_one_action_does_not_stop_the_rest(self):
        actions = [
            {"type": "create_issue", "number": 1},
            {"type": "create_issue", "number": 2},
        ]
        with (
            patch.object(P, "gh_put", side_effect=[GitHubError("403", 403), None]),
            patch("app.core.snapshot.take_snapshot", return_value="safe1"),
            patch(
                "app.core.snapshot.get_snapshot_by_number",
                return_value=_snapshot_with(actions),
            ),
        ):
            out = P.cmd_rollback("o/r", 1, "tok", "1 confirm", "dev")

        assert "Failed" in out
        assert "Closed issue #2" in out


class TestConfirmationGate:
    def test_preview_without_confirm_performs_nothing(self):
        with (
            patch.object(P, "gh_put") as put,
            patch("app.core.snapshot.take_snapshot") as snap,
            patch(
                "app.core.snapshot.get_snapshot_by_number",
                return_value=_snapshot_with([{"type": "create_issue", "number": 7}]),
            ),
        ):
            out = P.cmd_rollback("o/r", 1, "tok", "1", "dev")

        assert "Confirm Rollback" in out
        put.assert_not_called()
        snap.assert_not_called(), "a preview must not take a safety snapshot"

    def test_non_numeric_argument_is_rejected(self):
        out = P.cmd_rollback("o/r", 1, "tok", "banana", "dev")
        assert "Invalid Snapshot Number" in out

    def test_missing_snapshot_is_reported(self):
        with patch("app.core.snapshot.get_snapshot_by_number", return_value=None):
            out = P.cmd_rollback("o/r", 1, "tok", "99 confirm", "dev")
        assert "Not Found" in out


class TestSnapshotPayloadRobustness:
    """take_snapshot swallows exceptions and returns None, so any unguarded
    read turns an odd API response into "no snapshot" — silently."""

    def _gh(self, issues=None, prs=None, commits=None, repo=None):
        def _route(path, token, *a, **kw):
            if "/issues" in path:
                return issues if issues is not None else []
            if "/pulls" in path:
                return prs if prs is not None else []
            if "/commits" in path:
                return commits if commits is not None else []
            return repo if repo is not None else {"default_branch": "main"}

        return _route

    def test_pr_with_null_head_from_a_deleted_fork(self):
        with patch.object(S, "gh_get", side_effect=self._gh(prs=[{"number": 1, "head": None}])):
            assert S.take_snapshot("o/r", "tok") is not None

    def test_label_entries_without_a_name(self):
        issues = [{"number": 1, "title": "t", "labels": [{"color": "f00"}, "weird"]}]
        with patch.object(S, "gh_get", side_effect=self._gh(issues=issues)):
            assert S.take_snapshot("o/r", "tok") is not None

    def test_issue_missing_every_field(self):
        with patch.object(S, "gh_get", side_effect=self._gh(issues=[{}])):
            assert S.take_snapshot("o/r", "tok") is not None

    def test_error_dict_instead_of_a_list(self):
        """Iterating a dict yields its keys as strings, not issue objects."""
        err = {"message": "Not Found"}
        with patch.object(S, "gh_get", side_effect=self._gh(issues=err, prs=err, commits=err)):
            assert S.take_snapshot("o/r", "tok") is not None

    def test_commits_without_a_sha(self):
        with patch.object(S, "gh_get", side_effect=self._gh(commits=[{}])):
            snap_id = S.take_snapshot("o/r", "tok")
        assert snap_id is not None
        assert S.get_snapshot("o/r", snap_id)["state"]["latest_commit"] == ""

    def test_a_genuine_api_failure_still_returns_none(self):
        with patch.object(S, "gh_get", side_effect=RuntimeError("GitHub 500")):
            assert S.take_snapshot("o/r", "tok") is None


class TestSnapshotListing:
    def test_malformed_entry_does_not_hide_every_snapshot(self):
        """One KeyError used to escape the loop and return []."""
        from app.core.redis_client import get_redis

        import json

        r = get_redis()
        r.set("snapshot:o/r:good", json.dumps({"id": "good", "state": {"latest_commit": "a" * 40}}))
        r.set("snapshot:o/r:bad", json.dumps({"id": "bad"}))  # no "state"
        r.lpush("snapshot_index:o/r", "good")
        r.lpush("snapshot_index:o/r", "bad")

        listed = S.list_snapshots("o/r")
        assert len(listed) == 2, "a malformed snapshot must not hide the good one"

    def test_restore_examples_match_what_exists(self):
        """The examples were hardcoded to 1 and 2, so a repo with one snapshot
        was told to run `/rollback 2`, which cannot succeed."""
        with patch.object(S, "list_snapshots", return_value=[
            {"number": 1, "trigger": "manual", "timestamp": "2026-08-20T06:00",
             "issues_count": 0, "prs_count": 0, "commit": "abc1234", "bot_actions": 0}
        ]):
            out = S.format_snapshot_list("o/r")
        assert "/rollback 1" in out
        assert "/rollback 2" not in out

    def test_empty_repo_gets_the_no_snapshots_message(self):
        with patch.object(S, "list_snapshots", return_value=[]):
            assert "No Snapshots Available" in S.format_snapshot_list("o/r")
