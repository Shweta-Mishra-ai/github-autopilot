"""tests/test_notifications.py — app/github/notifications.py (Slack + Discord)."""

from unittest.mock import MagicMock, patch

import app.github.notifications as notif


class TestNotifyFilterAndRouting:
    def test_notify_skips_when_no_webhooks_configured(self, monkeypatch):
        monkeypatch.setattr(notif, "SLACK_ENABLED", False)
        monkeypatch.setattr(notif, "DISCORD_ENABLED", False)
        with patch("threading.Thread") as thread:
            notif.notify("t", "m")
        thread.assert_not_called()

    def test_notify_suppressed_by_event_filter(self, monkeypatch):
        monkeypatch.setattr(notif, "SLACK_ENABLED", True)
        monkeypatch.setitem(notif.NOTIFY_FILTER, "commit_lint", False)
        with patch("threading.Thread") as thread:
            notif.notify("t", "m", event_type="commit_lint")
        thread.assert_not_called()

    def test_notify_dispatches_slack_and_discord_threads(self, monkeypatch):
        monkeypatch.setattr(notif, "SLACK_ENABLED", True)
        monkeypatch.setattr(notif, "DISCORD_ENABLED", True)
        started = []

        class FakeThread:
            def __init__(self, target, args, daemon):
                self.target, self.args = target, args

            def start(self):
                started.append(self.target)

        with patch("threading.Thread", FakeThread):
            notif.notify("Title", "Message", repo="o/r")

        assert notif._send_slack in started
        assert notif._send_discord in started

    def test_notify_appends_repo_to_title(self, monkeypatch):
        monkeypatch.setattr(notif, "SLACK_ENABLED", True)
        monkeypatch.setattr(notif, "DISCORD_ENABLED", False)
        captured = {}

        class FakeThread:
            def __init__(self, target, args, daemon):
                captured["title"] = args[0]

            def start(self):
                pass

        with patch("threading.Thread", FakeThread):
            notif.notify("Title", "Message", repo="o/r")
        assert "o/r" in captured["title"]


class TestSendSlack:
    def test_send_slack_success(self, monkeypatch):
        monkeypatch.setattr(notif, "SLACK_WEBHOOK_URL", "https://hooks.slack.example/x")
        resp = MagicMock(status_code=200)
        with patch("app.github.notifications.requests.post", return_value=resp) as post:
            notif._send_slack("Title", "Message", "critical")
        assert post.called
        payload = post.call_args.kwargs["json"]
        assert payload["attachments"][0]["title"] == "Title"
        assert payload["attachments"][0]["footer"] == "GitHub Autopilot"

    def test_send_slack_handles_non_200(self, monkeypatch):
        monkeypatch.setattr(notif, "SLACK_WEBHOOK_URL", "https://hooks.slack.example/x")
        resp = MagicMock(status_code=500)
        with patch("app.github.notifications.requests.post", return_value=resp):
            notif._send_slack("Title", "Message", "warning")  # must not raise

    def test_send_slack_handles_exception(self, monkeypatch):
        monkeypatch.setattr(notif, "SLACK_WEBHOOK_URL", "https://hooks.slack.example/x")
        with patch("app.github.notifications.requests.post", side_effect=Exception("boom")):
            notif._send_slack("Title", "Message", "info")  # must not raise


class TestSendDiscord:
    def test_send_discord_builds_embed_with_fields_and_url(self, monkeypatch):
        monkeypatch.setattr(notif, "DISCORD_WEBHOOK_URL", "https://discord.example/x")
        resp = MagicMock(status_code=204)
        with patch("app.github.notifications.requests.post", return_value=resp) as post:
            notif._send_discord(
                "Title",
                "Description",
                "critical",
                [{"name": "K", "value": "V", "inline": True}],
                "https://example.com/pr/1",
            )
        payload = post.call_args.kwargs["json"]
        embed = payload["embeds"][0]
        assert embed["title"] == "Title"
        assert embed["url"] == "https://example.com/pr/1"
        assert embed["fields"][0]["name"] == "K"

    def test_send_discord_handles_failure_status(self, monkeypatch):
        monkeypatch.setattr(notif, "DISCORD_WEBHOOK_URL", "https://discord.example/x")
        resp = MagicMock(status_code=400, text="bad request")
        with patch("app.github.notifications.requests.post", return_value=resp):
            notif._send_discord("T", "D", "info", [], "")  # must not raise

    def test_send_discord_handles_exception(self, monkeypatch):
        monkeypatch.setattr(notif, "DISCORD_WEBHOOK_URL", "https://discord.example/x")
        with patch("app.github.notifications.requests.post", side_effect=Exception("boom")):
            notif._send_discord("T", "D", "info", [], "")  # must not raise


class TestNotificationHelpers:
    def test_notify_secret_detected_fields(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(notif, "notify", lambda **kw: captured.update(kw))
        notif.notify_secret_detected("o/r", 3)
        assert captured["event_type"] == "secret_detected"
        assert captured["severity"] == "critical"
        assert any(f["name"] == "Findings" for f in captured["fields"])

    def test_notify_high_risk_pr_includes_url(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(notif, "notify", lambda **kw: captured.update(kw))
        notif.notify_high_risk_pr("o/r", 7, "Some PR title")
        assert captured["url"] == "https://github.com/o/r/pull/7"

    def test_notify_vulnerability_severity_mapping(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(notif, "notify", lambda **kw: captured.update(kw))
        notif.notify_vulnerability("o/r", "lodash", "HIGH", "CVE-1")
        assert captured["severity"] == "critical"
        notif.notify_vulnerability("o/r", "lodash", "LOW", "CVE-2")
        assert captured["severity"] == "warning"

    def test_notify_new_issue_formats_labels(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(notif, "notify", lambda **kw: captured.update(kw))
        notif.notify_new_issue("o/r", 5, "title", ["bug", "urgent"])
        label_field = next(f for f in captured["fields"] if f["name"] == "Labels")
        assert "bug" in label_field["value"] and "urgent" in label_field["value"]

    def test_notify_new_issue_no_labels(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(notif, "notify", lambda **kw: captured.update(kw))
        notif.notify_new_issue("o/r", 5, "title", [])
        label_field = next(f for f in captured["fields"] if f["name"] == "Labels")
        assert label_field["value"] == "none"

    def test_notify_all_providers_down_includes_breaker_status(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(notif, "notify", lambda **kw: captured.update(kw))
        with patch(
            "app.ai.circuit_breaker.status_all",
            return_value={"groq_70b": {"state": "open", "recovers_in_seconds": 30}},
        ):
            notif.notify_all_providers_down()
        assert captured["fields"][0]["name"] == "groq_70b"
        assert "recovers in 30s" in captured["fields"][0]["value"]

    def test_notify_all_providers_down_survives_status_error(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(notif, "notify", lambda **kw: captured.update(kw))
        with patch("app.ai.circuit_breaker.status_all", side_effect=Exception("boom")):
            notif.notify_all_providers_down()  # must not raise
        assert captured["fields"] == []


class TestDiscordTestAndRichSend:
    def test_test_discord_disabled(self, monkeypatch):
        monkeypatch.setattr(notif, "DISCORD_ENABLED", False)
        ok, msg = notif.test_discord()
        assert ok is False
        assert "not set" in msg

    def test_test_discord_success(self, monkeypatch):
        monkeypatch.setattr(notif, "DISCORD_ENABLED", True)
        monkeypatch.setattr(notif, "DISCORD_WEBHOOK_URL", "https://discord.example/x")
        resp = MagicMock(status_code=200)
        with patch("app.github.notifications.requests.post", return_value=resp):
            ok, msg = notif.test_discord()
        assert ok is True

    def test_test_discord_failure_status(self, monkeypatch):
        monkeypatch.setattr(notif, "DISCORD_ENABLED", True)
        monkeypatch.setattr(notif, "DISCORD_WEBHOOK_URL", "https://discord.example/x")
        resp = MagicMock(status_code=500, text="server error")
        with patch("app.github.notifications.requests.post", return_value=resp):
            ok, msg = notif.test_discord()
        assert ok is False
        assert "500" in msg

    def test_test_discord_exception(self, monkeypatch):
        monkeypatch.setattr(notif, "DISCORD_ENABLED", True)
        monkeypatch.setattr(notif, "DISCORD_WEBHOOK_URL", "https://discord.example/x")
        with patch("app.github.notifications.requests.post", side_effect=Exception("net down")):
            ok, msg = notif.test_discord()
        assert ok is False
        assert "net down" in msg

    def test_send_rich_discord_without_webhook_configured(self, monkeypatch):
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
        ok, msg = notif.send_rich_discord("t", "d")
        assert ok is False
        assert "not set" in msg

    def test_send_rich_discord_success_with_fields_and_url(self, monkeypatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/x")
        resp = MagicMock(status_code=204)
        with patch("app.github.notifications.requests.post", return_value=resp) as post:
            ok, msg = notif.send_rich_discord(
                "t", "d", fields=[{"name": "K", "value": "V"}], url="https://x"
            )
        assert ok is True
        payload = post.call_args.kwargs["json"]
        assert payload["embeds"][0]["url"] == "https://x"

    def test_send_rich_discord_exception(self, monkeypatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/x")
        with patch("app.github.notifications.requests.post", side_effect=Exception("boom")):
            ok, msg = notif.send_rich_discord("t", "d")
        assert ok is False

    def test_notify_autofix_created_calls_rich_discord(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            notif, "send_rich_discord", lambda **kw: captured.update(kw) or (True, "ok")
        )
        notif.notify_autofix_created("o/r", 1, 2, "https://pr")
        assert captured["url"] == "https://pr"

    def test_notify_weekly_report_maps_grade_to_color(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            notif, "send_rich_discord", lambda **kw: captured.update(kw) or (True, "ok")
        )
        notif.notify_weekly_report("o/r", "A", merged=3, closed=1)
        assert captured["color"] == 0x2ECC71
