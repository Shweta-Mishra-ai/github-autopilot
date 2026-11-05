"""Audit round 2 (C19): /merge and /notify gaps."""

from unittest.mock import MagicMock, patch

import pytest


def _cfg(over=None):
    values = {
        ("auto_merge", "require_no_blocking_reviews"): True,
        ("auto_merge", "require_passing_checks"): True,
        ("auto_merge", "allow_protected_branches"): True,
        ("auto_merge", "allowed_risk_levels"): [],
    }
    values.update(over or {})
    cfg = MagicMock()
    cfg.auto_merge_enabled.return_value = True
    cfg.get.side_effect = lambda *k, default=None: values.get(k, default)
    return cfg


class TestZeroChecksIsNotPassing:
    PR = {"mergeable": True, "draft": False, "commits": 1, "base": {"ref": "dev"}}

    def test_no_checks_and_no_statuses_refuses(self):
        from app.core.guardrails import check_pr_auto_merge

        out = check_pr_auto_merge(self.PR, [], [], _cfg(), statuses=[])
        assert not out.passed
        assert "require_passing_checks" in out.reason

    def test_a_repo_without_ci_can_opt_out(self):
        from app.core.guardrails import check_pr_auto_merge

        cfg = _cfg({("auto_merge", "require_passing_checks"): False})
        assert check_pr_auto_merge(self.PR, [], [], cfg, statuses=[]).passed


class TestMergeReadsEveryReview:
    def test_a_change_request_on_the_second_page_blocks(self):
        from app.handlers.comments import publisher as P

        approved = [{"user": {"login": f"u{i}"}, "state": "APPROVED"} for i in range(100)]
        blocking = [{"user": {"login": "boss"}, "state": "CHANGES_REQUESTED"}]

        def get(path, token):
            if "/reviews" in path:
                return approved if path.endswith("page=1") else blocking
            if "/status" in path:
                return {"statuses": [{"state": "success", "context": "ci"}]}
            return {
                "number": 7,
                "state": "open",
                "mergeable": True,
                "draft": False,
                "commits": 1,
                "head": {"sha": "abc12345", "ref": "f"},
                "base": {"ref": "dev"},
            }

        with (
            patch.object(P, "gh_get", side_effect=get),
            patch.object(P, "_all_check_runs", return_value=([{"conclusion": "success"}], True)),
            patch.object(P, "gh_put") as put,
        ):
            out = P.cmd_merge("o/r", 7, {"pull_request": {}}, "t", "me", _cfg())
        assert "boss" in out
        put.assert_not_called()


@pytest.mark.parametrize(
    "pr, expected",
    [({"merged": True, "state": "closed"}, "Already Merged"), ({"merged": False, "state": "closed"}, "Closed")],
)
def test_merged_or_closed_pr_says_so(pr, expected):
    from app.handlers.comments import publisher as P

    with patch.object(P, "gh_get", return_value=pr):
        out = P.cmd_merge("o/r", 7, {"pull_request": {}}, "t", "me", _cfg())
    assert expected in out


def test_notify_honours_a_repo_that_disabled_the_channel(monkeypatch):
    from app.handlers.comments import integrations

    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/x")
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    cfg = MagicMock()
    cfg.get.side_effect = lambda *k, default=None: False if k == ("notifications", "discord") else default
    with patch("app.github.notifications.send_rich_discord") as send:
        out = integrations.cmd_notify("o/r", 1, {"title": "t"}, "tok", "", cfg)
    send.assert_not_called()
    assert "Disabled" in out
