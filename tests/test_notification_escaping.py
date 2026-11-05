"""
Round-3 audit C7: issue and PR titles, labels and the /notify note are
anyone's text, and reached Slack and Discord as written — a title could ping
the whole channel or render a disguised link.
"""

from unittest.mock import MagicMock, patch

from app.github import notifications as N

HOSTILE = "<!channel> @everyone [Reset your password](https://evil.example) <https://evil.example|Docs>"


def _sent(fn, *args, **kwargs):
    resp = MagicMock(status_code=200, text="")
    with patch.object(N.requests, "post", return_value=resp) as post:
        fn(*args, **kwargs)
    return post.call_args.kwargs["json"]


def test_slack_text_cannot_mention_or_link(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/x")
    body = str(_sent(N._send_slack, "New Issue Opened", f"Issue #1: {HOSTILE}", "info"))
    assert "<!channel>" not in body and "<https://" not in body
    assert "&lt;!channel&gt;" in body


def test_discord_never_pings_and_masked_links_break(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/api/webhooks/1/x")
    payload = _sent(
        N._send_discord, "New Issue Opened", f"Issue #1: {HOSTILE}", "info", [{"name": "T", "value": HOSTILE}], ""
    )
    assert payload["allowed_mentions"] == {"parse": []}
    embed = payload["embeds"][0]
    assert "](https://evil" not in embed["description"]
    assert "](https://evil" not in embed["fields"][0]["value"]


def test_the_bots_own_link_still_works(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/api/webhooks/1/x")
    payload = _sent(N.notify_autofix_created, "o/r", 1, 2, "https://github.com/o/r/pull/2")
    assert "[#2](https://github.com/o/r/pull/2)" in str(payload["embeds"][0]["fields"])
    assert payload["allowed_mentions"] == {"parse": []}


def test_slash_notify_escapes_the_title_note_and_labels(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/api/webhooks/1/x")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/x")
    from app.core.config import Config
    from app.handlers.comments.integrations import cmd_notify

    issue = {"title": HOSTILE, "labels": [{"name": "[x](https://evil.example)"}], "html_url": "u"}
    resp = MagicMock(status_code=200, text="")
    with patch.object(N.requests, "post", return_value=resp) as post:
        cmd_notify("o/r", 1, issue, "tok", HOSTILE, Config({}))
    payloads = [c.kwargs["json"] for c in post.call_args_list]
    assert len(payloads) == 2
    discord = next(p for p in payloads if "embeds" in p)
    slack = next(p for p in payloads if "attachments" in p)
    assert "](https://evil" not in str(payloads)
    assert discord["allowed_mentions"] == {"parse": []}
    # `<!channel>` is Slack syntax; Discord shows it as text.
    assert "<!channel>" not in str(slack) and "<https://evil" not in str(slack)
