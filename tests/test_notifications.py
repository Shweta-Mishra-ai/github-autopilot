"""tests/test_notifications.py — app/github/notifications.py (Slack + Discord)."""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import app.github.notifications as notif


class TestNotifyFilterAndRouting:
    def test_notify_skips_when_no_webhooks_configured(self, monkeypatch):
        monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
        with patch("threading.Thread") as thread:
            notif.notify("t", "m")
        thread.assert_not_called()

    def test_notify_suppressed_by_event_filter(self, monkeypatch):
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T0/B0/xxxx")
        monkeypatch.setitem(notif.NOTIFY_FILTER, "commit_lint", False)
        with patch("threading.Thread") as thread:
            notif.notify("t", "m", event_type="commit_lint")
        thread.assert_not_called()

    def test_notify_dispatches_slack_and_discord_threads(self, monkeypatch):
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T0/B0/xxxx")
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/xxxx")
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
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T0/B0/xxxx")
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
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
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/x")
        resp = MagicMock(status_code=200)
        with patch("app.github.notifications.requests.post", return_value=resp) as post:
            notif._send_slack("Title", "Message", "critical")
        assert post.called
        payload = post.call_args.kwargs["json"]
        assert payload["attachments"][0]["title"] == "Title"
        assert payload["attachments"][0]["footer"] == "GitHub Autopilot"

    def test_send_slack_handles_non_200(self, monkeypatch):
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/x")
        resp = MagicMock(status_code=500)
        with patch("app.github.notifications.requests.post", return_value=resp):
            notif._send_slack("Title", "Message", "warning")  # must not raise

    def test_send_slack_handles_exception(self, monkeypatch):
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/x")
        with patch("app.github.notifications.requests.post", side_effect=Exception("boom")):
            notif._send_slack("Title", "Message", "info")  # must not raise


class TestSendDiscord:
    def test_send_discord_builds_embed_with_fields_and_url(self, monkeypatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/x")
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
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/x")
        resp = MagicMock(status_code=400, text="bad request")
        with patch("app.github.notifications.requests.post", return_value=resp):
            notif._send_discord("T", "D", "info", [], "")  # must not raise

    def test_send_discord_handles_exception(self, monkeypatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/x")
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
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
        ok, msg = notif.test_discord()
        assert ok is False
        assert "not set" in msg

    def test_test_discord_success(self, monkeypatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/xxxx")
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/x")
        resp = MagicMock(status_code=200)
        with patch("app.github.notifications.requests.post", return_value=resp):
            ok, msg = notif.test_discord()
        assert ok is True

    def test_test_discord_failure_status(self, monkeypatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/xxxx")
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/x")
        resp = MagicMock(status_code=500, text="server error")
        with patch("app.github.notifications.requests.post", return_value=resp):
            ok, msg = notif.test_discord()
        assert ok is False
        assert "500" in msg

    def test_test_discord_exception(self, monkeypatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/xxxx")
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/x")
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


class TestConfiguredWebhookActuallyDelivers:
    """
    The bug this class exists for: DEFAULTS["notifications"]["slack"] and
    ["discord"] were both False, and notify() ANDs the env var with the repo
    config. Every handler that loads a repo config and passes it — secret
    detection, high-risk PRs, all-providers-down — was therefore silently
    suppressed. The operator set a valid webhook URL and nothing ever arrived,
    with only a debug line to show for it.

    The env var is the master switch (does this deployment have the channel);
    the config keys are per-repo overrides on top of it.
    """

    @staticmethod
    def _default_config():
        import copy

        from app.core.config import DEFAULTS, Config

        return Config(copy.deepcopy(DEFAULTS))

    @pytest.fixture
    def channels(self, monkeypatch):
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/x")
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/x")

    def test_default_repo_config_does_not_suppress_delivery(self, channels):
        with (
            patch.object(notif, "_send_slack") as slack,
            patch.object(notif, "_send_discord") as discord,
        ):
            notif.notify(
                "t", "m", severity="critical", repo="o/r",
                event_type="secret_detected", config=self._default_config(),
            )
            for t in threading.enumerate():
                if t is not threading.current_thread() and not t.daemon:
                    t.join(timeout=2)
            time.sleep(0.2)

        assert slack.called, "a configured Slack webhook must deliver by default"
        assert discord.called, "a configured Discord webhook must deliver by default"

    def test_repo_can_still_opt_out_of_one_channel(self, channels):
        import copy

        from app.core.config import DEFAULTS, Config

        data = copy.deepcopy(DEFAULTS)
        data["notifications"]["slack"] = False
        with (
            patch.object(notif, "_send_slack") as slack,
            patch.object(notif, "_send_discord") as discord,
        ):
            notif.notify("t", "m", repo="o/r", config=Config(data))
            time.sleep(0.2)

        assert not slack.called, "an explicit opt-out must still be honoured"
        assert discord.called

    def test_no_webhook_url_means_no_delivery(self, monkeypatch):
        monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
        with (
            patch.object(notif, "_send_slack") as slack,
            patch.object(notif, "_send_discord") as discord,
        ):
            notif.notify("t", "m", repo="o/r", config=self._default_config())
            time.sleep(0.1)
        assert not slack.called
        assert not discord.called


class TestWebhookUrlIsReadAtCallTime:
    """Module-level constants froze the URL at first import, so a value set or
    changed afterwards was never seen — while send_rich_discord() read the
    environment per call, so one file disagreed with itself."""

    def test_slack_url_reflects_a_later_change(self, monkeypatch):
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://first.example/x")
        assert notif.slack_webhook_url() == "https://first.example/x"
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://second.example/x")
        assert notif.slack_webhook_url() == "https://second.example/x"

    def test_enabled_follows_the_env_var(self, monkeypatch):
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
        assert notif.discord_enabled() is False
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/x")
        assert notif.discord_enabled() is True

    def test_whitespace_only_url_is_not_enabled(self, monkeypatch):
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "   ")
        assert notif.slack_enabled() is False


class TestDeliveryIsObservable:
    """Sends run on daemon threads, so a failing webhook only ever produced a
    log line in a thread nobody reads."""

    def test_success_is_counted(self, monkeypatch):
        from app.core.metrics import metrics

        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/x")
        before = metrics.get("notifications.slack.sent", 0)
        with patch("app.github.notifications.requests.post", return_value=MagicMock(status_code=200)):
            notif._send_slack("t", "m", "info")
        assert metrics.get("notifications.slack.sent", 0) == before + 1

    def test_failure_status_is_counted(self, monkeypatch):
        from app.core.metrics import metrics

        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/x")
        before = metrics.get("notifications.discord.failed", 0)
        resp = MagicMock(status_code=400, text="bad payload")
        with patch("app.github.notifications.requests.post", return_value=resp):
            notif._send_discord("t", "m", "info", [], "")
        assert metrics.get("notifications.discord.failed", 0) == before + 1

    def test_network_error_is_counted(self, monkeypatch):
        from app.core.metrics import metrics

        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/x")
        before = metrics.get("notifications.slack.failed", 0)
        with patch("app.github.notifications.requests.post", side_effect=OSError("dns")):
            notif._send_slack("t", "m", "info")
        assert metrics.get("notifications.slack.failed", 0) == before + 1


class TestNotifyCommandDeliversToEveryConfiguredChannel:
    """
    /notify explicitly accepts a Slack-only configuration -- it passes the
    "no webhooks configured" guard when only SLACK_WEBHOOK_URL is set -- but
    then called send_rich_discord() and nothing else. There was no Slack
    sender to call.

    Worse, the success message was built from the list of CONFIGURED channels
    rather than delivered ones, so a Slack-only setup was told "Alert posted
    to: Slack" having sent nothing at all.
    """

    ISSUE = {
        "title": "Prod is down",
        "labels": [{"name": "bug"}],
        "html_url": "https://github.com/o/r/issues/5",
    }

    def _run(self, env, status=200):
        import os

        from app.handlers.comments.publisher import cmd_notify

        with (
            patch.dict(os.environ, env, clear=True),
            patch(
                "app.github.notifications.requests.post",
                return_value=MagicMock(status_code=status, text="ok"),
            ) as post,
        ):
            out = cmd_notify("o/r", 5, self.ISSUE, "tok", "")
        hosts = sorted({c.args[0].split("//")[1].split("/")[0] for c in post.call_args_list})
        return out, hosts

    def test_slack_only_actually_posts_to_slack(self):
        out, hosts = self._run({"SLACK_WEBHOOK_URL": "https://hooks.slack.example/x"})
        assert hosts == ["hooks.slack.example"], "Slack-only config sent nothing to Slack"
        assert "Delivered to: **Slack**" in out

    def test_discord_only_actually_posts_to_discord(self):
        out, hosts = self._run({"DISCORD_WEBHOOK_URL": "https://discord.example/x"})
        assert hosts == ["discord.example"]
        assert "Delivered to: **Discord**" in out

    def test_both_channels_each_receive_a_post(self):
        out, hosts = self._run(
            {
                "SLACK_WEBHOOK_URL": "https://hooks.slack.example/x",
                "DISCORD_WEBHOOK_URL": "https://discord.example/x",
            }
        )
        assert hosts == ["discord.example", "hooks.slack.example"]
        assert "Discord" in out and "Slack" in out

    def test_no_channel_is_claimed_that_did_not_receive_anything(self):
        """The report must reflect delivery, not configuration."""
        out, _ = self._run(
            {
                "SLACK_WEBHOOK_URL": "https://hooks.slack.example/x",
                "DISCORD_WEBHOOK_URL": "https://discord.example/x",
            },
            status=500,
        )
        assert "Delivered to" not in out
        assert "Not delivered" in out
        assert "Slack" in out and "Discord" in out

    def test_unconfigured_returns_setup_guidance(self):
        import os

        from app.handlers.comments.publisher import cmd_notify

        with patch.dict(os.environ, {}, clear=True):
            out = cmd_notify("o/r", 5, self.ISSUE, "tok", "")
        assert "Not Configured" in out


class TestRichSlackSender:
    def test_reports_failure_when_unconfigured(self, monkeypatch):
        monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
        ok, msg = notif.send_rich_slack("t", "d")
        assert ok is False
        assert "not set" in msg

    def test_success(self, monkeypatch):
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/x")
        with patch(
            "app.github.notifications.requests.post",
            return_value=MagicMock(status_code=200),
        ):
            ok, _ = notif.send_rich_slack("t", "d")
        assert ok is True

    def test_fields_and_link_are_rendered(self, monkeypatch):
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/x")
        with patch(
            "app.github.notifications.requests.post",
            return_value=MagicMock(status_code=200),
        ) as post:
            notif.send_rich_slack(
                "t", "d", fields=[{"name": "Repo", "value": "o/r"}], url="https://x/y"
            )
        att = post.call_args.kwargs["json"]["attachments"][0]
        assert att["title_link"] == "https://x/y"
        assert att["fields"][0]["title"] == "Repo"

    def test_network_error_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/x")
        with patch("app.github.notifications.requests.post", side_effect=OSError("dns")):
            ok, msg = notif.send_rich_slack("t", "d")
        assert ok is False
        assert "dns" in msg
