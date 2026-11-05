"""Audit round 2 (C7): nothing ever recorded a bot action, so /rollback
always reported "Rollback Complete" having undone nothing."""

from unittest.mock import patch

from app.core import snapshot


def test_actions_in_one_hour_share_one_snapshot(fake_redis):
    snapshot.record_action("o/r", {"type": "create_issue", "number": 5, "title": "t"})
    snapshot.record_action("o/r", {"type": "add_labels", "number": 6, "labels": ["bug 🐛"]})
    snaps = snapshot.list_snapshots("o/r")
    assert len(snaps) == 1
    assert snaps[0]["trigger"] == "bot_activity"
    assert snaps[0]["bot_actions"] == 2
    assert "—" in snapshot.format_snapshot_list("o/r")


def test_rollback_undoes_recorded_actions_newest_first(fake_redis):
    from app.handlers.comments import publisher as P

    snapshot.record_action("o/r", {"type": "create_issue", "number": 5, "title": "alert"})
    snapshot.record_action(
        "o/r",
        {"type": "edit_pr_title", "number": 9, "old_title": "orig", "old_body": "b",
         "new_title": "bot title", "new_body": "bot body"},
    )
    snapshot.record_action("o/r", {"type": "add_labels", "number": 6, "labels": ["priority: high 🔥"]})

    def current(path, token):
        # Still what the bot wrote; the issue is still open.
        return {"title": "bot title", "body": "bot body"} if "/pulls/" in path else {"state": "open"}

    calls = []
    with (
        patch.object(P, "gh_get", side_effect=current),
        patch.object(P, "gh_patch", side_effect=lambda p, t, d: calls.append(("patch", p, d))),
        patch.object(P, "gh_delete", side_effect=lambda p, t: calls.append(("delete", p))),
    ):
        out = P.cmd_rollback("o/r", 1, "tok", "1 confirm", "me")

    assert [c[0] for c in calls] == ["delete", "patch", "patch"]
    assert calls[0][1].endswith("/labels/priority%3A%20high%20%F0%9F%94%A5")
    assert calls[1][2] == {"title": "orig", "body": "b"}
    assert calls[2][2] == {"state": "closed"}
    assert "Closed issue #5" in out


def test_triage_records_only_labels_it_added():
    """A label the author set themselves must not be removed by /rollback."""
    from unittest.mock import MagicMock

    from app.handlers import issues

    triage = {
        "type": "bug", "priority": "high", "complexity": "moderate", "time_estimate": "",
        "labels": ["bug 🐛"], "welcome": "hi", "needs_info": False, "questions": [],
    }
    cfg = MagicMock()
    cfg.issues_enabled.return_value = True
    cfg.get.side_effect = lambda *a, **kw: kw.get("default", True)
    cfg.footer = ""
    payload = {
        "action": "opened",
        "issue": {"number": 42, "title": "crash", "body": "b", "user": {"login": "u"},
                  "labels": [{"name": "bug 🐛"}]},
        "repository": {"full_name": "o/r"},
        "installation": {"id": 1},
    }
    with (
        patch.object(issues, "get_installation_token", return_value="tok"),
        patch.object(issues, "load_config", return_value=cfg),
        patch.object(issues, "gh_get", return_value={"language": "Python"}),
        patch.object(issues.router, "ask", return_value=(triage, MagicMock())),
        patch.object(issues, "validate_issue_triage", return_value=triage),
        patch.object(issues, "check_auto_label", return_value=MagicMock(passed=True)),
        patch.object(issues, "gh_post"),
        patch.object(issues, "notify_new_issue"),
        patch.object(issues, "_ensure_labels"),
        patch("app.core.snapshot.record_action") as rec,
    ):
        issues.handle(payload)
    rec.assert_called_once_with(
        "o/r", {"type": "add_labels", "number": 42, "labels": ["priority: high 🔥"]}
    )
