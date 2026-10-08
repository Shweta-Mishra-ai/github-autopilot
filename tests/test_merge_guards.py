"""
/merge guard regressions. Each was reproduced against the code before the fix.
"""

from unittest.mock import patch

from app.core.guardrails import check_pr_auto_merge
from app.handlers.comments import publisher


class _Cfg:
    def __init__(self, **auto_merge):
        self._am = {"allowed_risk_levels": [], **auto_merge}

    def auto_merge_enabled(self):
        return True

    def get(self, section, key, default=None):
        return self._am.get(key, default) if section == "auto_merge" else default


PR = {
    "mergeable": True,
    "base": {"ref": "feature/x", "repo": {"full_name": "o/r"}},
    "head": {"sha": "abc123", "ref": "feat/y", "repo": {"full_name": "o/r"}},
    "commits": 1,
    "draft": False,
    "number": 7,
}
OK_CHECK = {"name": "CI", "status": "completed", "conclusion": "success"}


class TestGuard:
    def test_a_check_still_running_blocks(self):
        running = {"name": "slow-tests", "status": "in_progress", "conclusion": None}
        r = check_pr_auto_merge(PR, [OK_CHECK, running], [], _Cfg())
        assert not r.passed and "still running" in r.reason and "slow-tests" in r.reason

    def test_a_failing_commit_status_blocks(self):
        statuses = [{"context": "ci/circleci", "state": "failure"}]
        r = check_pr_auto_merge(PR, [OK_CHECK], [], _Cfg(), statuses=statuses)
        assert not r.passed and "ci/circleci" in r.reason

    def test_a_pending_commit_status_blocks(self):
        statuses = [{"context": "deploy-preview", "state": "pending"}]
        assert not check_pr_auto_merge(PR, [OK_CHECK], [], _Cfg(), statuses=statuses).passed

    def test_a_reviewer_who_later_approved_no_longer_blocks(self):
        reviews = [
            {"state": "CHANGES_REQUESTED", "user": {"login": "ana"}},
            {"state": "COMMENTED", "user": {"login": "ana"}},
            {"state": "APPROVED", "user": {"login": "ana"}},
        ]
        assert check_pr_auto_merge(PR, [OK_CHECK], reviews, _Cfg()).passed

    def test_an_outstanding_change_request_still_blocks(self):
        reviews = [
            {"state": "APPROVED", "user": {"login": "ana"}},
            {"state": "CHANGES_REQUESTED", "user": {"login": "ana"}},
        ]
        r = check_pr_auto_merge(PR, [OK_CHECK], reviews, _Cfg())
        assert not r.passed and "@ana" in r.reason


def _merge(pr, check_pages, statuses=None, cfg=None):
    calls = [pr, []] + list(check_pages) + [{"statuses": statuses or []}]
    with (
        patch.object(publisher, "gh_get", side_effect=calls) as get,
        patch.object(publisher, "gh_put", return_value={"merged": True, "sha": "deadbeef"}) as put,
        patch.object(publisher, "gh_delete") as delete,
    ):
        out = publisher.cmd_merge("o/r", 7, {"pull_request": {}}, "tok", "alice", cfg or _Cfg())
    return out, get, put, delete


class TestCmdMerge:
    def test_merges_exactly_the_checked_commit(self):
        out, _, put, _ = _merge(PR, [{"total_count": 1, "check_runs": [OK_CHECK]}])
        assert "Merged" in out
        assert put.call_args.args[2]["sha"] == "abc123"

    def test_a_fork_branch_is_not_deleted_from_this_repository(self):
        fork_pr = {**PR, "head": {**PR["head"], "ref": "develop", "repo": {"full_name": "x/fork"}}}
        out, _, _, delete = _merge(fork_pr, [{"total_count": 1, "check_runs": [OK_CHECK]}])
        assert "Merged" in out
        delete.assert_not_called()

    def test_a_same_repo_branch_is_still_cleaned_up(self):
        _, _, _, delete = _merge(PR, [{"total_count": 1, "check_runs": [OK_CHECK]}])
        delete.assert_called_once()
        assert delete.call_args.args[0].endswith("/git/refs/heads/feat/y")

    def test_a_failing_check_on_the_second_page_is_seen(self):
        page1 = {"total_count": 101, "check_runs": [OK_CHECK] * 100}
        page2 = {
            "total_count": 101,
            "check_runs": [{"name": "late", "status": "completed", "conclusion": "failure"}],
        }
        out, _, put, _ = _merge(PR, [page1, page2])
        assert "Cannot Merge" in out and "late" in out
        put.assert_not_called()

    def test_refuses_when_not_every_check_could_be_read(self):
        partial = {"total_count": 900, "check_runs": [OK_CHECK] * 100}
        with (
            patch.object(publisher, "gh_get", side_effect=[PR, []] + [partial] * 5),
            patch.object(publisher, "gh_put") as put,
        ):
            out = publisher.cmd_merge("o/r", 7, {"pull_request": {}}, "tok", "alice", _Cfg())
        assert "could not read every check run" in out
        put.assert_not_called()
