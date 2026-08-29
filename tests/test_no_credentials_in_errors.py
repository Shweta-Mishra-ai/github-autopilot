"""
tests/test_no_credentials_in_errors.py

A provider that takes its key as a URL query parameter puts that key into
every exception `requests` raises, because those messages quote the URL:

    HTTPSConnectionPool(host='generativelanguage.googleapis.com', port=443):
    Max retries exceeded with url:
    /v1beta/models/gemini-1.5-flash:generateContent?key=AIzaSy...

Gemini built exactly that URL. The message was assigned to LLMResponse.error
and logged by the router as `router.primary_failed ... error=...`, so a single
connection blip wrote the API key into the deployment's logs in plaintext —
and Render keeps those.

Two independent guards, because the cost of being wrong once is a rotated
credential: the key travels in a header now, and anything that still reaches
an error string is redacted.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from app.ai.providers.base import redact_secrets

SECRET = "AIzaSyD-NotARealKey-ButShapedLikeOne-0123"


class TestTheKeyIsNotInTheUrl:

    def test_gemini_sends_the_key_as_a_header(self, monkeypatch):
        from app.ai.providers import gemini as gem

        monkeypatch.setenv("GEMINI_API_KEY", SECRET)
        breaker = MagicMock()
        breaker.is_available.return_value = True

        ok = MagicMock()
        ok.status_code = 200
        ok.headers = {}
        ok.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "hi"}]}}],
            "usageMetadata": {},
        }

        with (
            patch.object(gem, "get_breaker", return_value=breaker),
            patch.object(gem.http_requests, "post", return_value=ok) as post,
        ):
            gem.GeminiProvider().call_raw("sys", "user", 100, 0.2, 30)

        args, kwargs = post.call_args
        assert SECRET not in args[0], "the key is back in the URL — every exception will quote it"
        assert kwargs["headers"]["x-goog-api-key"] == SECRET

    @pytest.mark.parametrize(
        "exc",
        [
            requests.exceptions.ConnectionError(
                "HTTPSConnectionPool(host='generativelanguage.googleapis.com', port=443): "
                "Max retries exceeded with url: /v1beta/models/gemini-1.5-flash:"
                f"generateContent?key={SECRET} (Caused by NewConnectionError())"
            ),
            requests.exceptions.SSLError(
                f"Failed for url: https://generativelanguage.googleapis.com/x?key={SECRET}"
            ),
        ],
    )
    def test_a_key_never_reaches_the_error_or_the_breaker(self, exc, monkeypatch):
        """The net under the header change: a redirect, or a future edit, must
        not be able to put a credential into a string that gets logged."""
        from app.ai.providers import gemini as gem

        monkeypatch.setenv("GEMINI_API_KEY", SECRET)
        breaker = MagicMock()
        breaker.is_available.return_value = True

        with (
            patch.object(gem, "get_breaker", return_value=breaker),
            patch.object(gem.http_requests, "post", side_effect=exc),
        ):
            result = gem.GeminiProvider().call_raw("sys", "user", 100, 0.2, 30)

        assert SECRET not in (result.error or ""), f"key leaked into error: {result.error!r}"
        for call in breaker.record_failure.call_args_list:
            assert SECRET not in str(call), "key leaked into the breaker's failure reason"

    def test_openrouter_errors_are_redacted_too(self, monkeypatch):
        from app.ai.providers import openrouter as orx

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret")
        breaker = MagicMock()
        breaker.is_available.return_value = True
        exc = requests.exceptions.ConnectionError(f"url: https://openrouter.ai/x?api_key={SECRET}")

        with (
            patch.object(orx, "get_breaker", return_value=breaker),
            patch.object(orx.http_requests, "post", side_effect=exc),
        ):
            _result, meta = orx.OpenRouterProvider().ask("sys", "user")

        assert SECRET not in (meta.error or "")


class TestRedactSecrets:

    @pytest.mark.parametrize(
        "param", ["key", "api_key", "apikey", "access_token", "token", "API_KEY", "Key"]
    )
    def test_every_credential_query_parameter_is_covered(self, param):
        assert SECRET not in redact_secrets(f"https://x/y?{param}={SECRET}")

    def test_it_stops_at_the_parameter_boundary(self):
        """Redacting past `&` would eat the rest of the message."""
        out = redact_secrets(f"https://x/y?key={SECRET}&model=gpt-oss-120b failed")
        assert "model=gpt-oss-120b failed" in out
        assert SECRET not in out

    def test_ordinary_text_is_untouched(self):
        assert redact_secrets("connection refused") == "connection refused"

    def test_none_and_empty_are_safe(self):
        assert redact_secrets(None) == ""
        assert redact_secrets("") == ""


class TestWebhookUrlsAreCredentialsToo:
    """
    A Slack or Discord webhook URL is not an address, it is a bearer token:
    whoever holds it can post into the channel as the bot. Both senders
    logged the exception verbatim, and requests quotes the URL it failed on —
    so one connection error published the webhook into the deployment's logs.
    """

    # Assembled rather than written out: as a single literal the Slack one
    # matches GitHub's push protection, which blocks the push. It is a made-up
    # value, but a fixture that trips a real secret scanner is a fixture that
    # will be silenced or exempted one day, and an exemption on this file is
    # the last thing this repository needs.
    _SLACK_HOST = "https://hooks." + "slack.com/services"
    SLACK = f"{_SLACK_HOST}/T00000000/B11111111/" + "SeCrEt" + "WebhookToken123456"
    DISCORD = "https://discord.com/api/webhooks/123456789/" + "DiScOrD" + "hOoKtOkEn-abcdefg"
    TEAMS = "https://acme.webhook.office.com/webhookb2/aaaa-bbbb/IncomingWebhook/ccc/ddd"

    @pytest.mark.parametrize("url_attr", ["SLACK", "DISCORD", "TEAMS"])
    def test_the_token_is_redacted(self, url_attr):
        url = getattr(self, url_attr)
        out = redact_secrets(f"Max retries exceeded with url: {url}")
        secret_tail = url.split("/", 4)[-1]
        assert secret_tail not in out, out

    def test_the_host_survives_so_the_log_still_says_which_one_failed(self):
        out = redact_secrets(f"failed: {self.SLACK}")
        assert "hooks.slack.com" in out
        assert "REDACTED" in out

    @pytest.mark.parametrize("channel", ["slack", "discord"])
    def test_a_send_failure_does_not_log_the_webhook(self, channel, monkeypatch, caplog):
        import logging

        import app.github.notifications as n

        url = self.SLACK if channel == "slack" else self.DISCORD
        monkeypatch.setenv("SLACK_WEBHOOK_URL", self.SLACK)
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", self.DISCORD)
        exc = requests.exceptions.ConnectionError(
            f"HTTPSConnectionPool(host='x', port=443): Max retries exceeded with url: {url}"
        )

        send, args = (
            (n._send_slack, ("t", "m", "critical"))
            if channel == "slack"
            else (n._send_discord, ("t", "m", "critical", [], "http://x"))
        )

        with caplog.at_level(logging.ERROR), patch.object(n.requests, "post", side_effect=exc):
            send(*args)

        logged = caplog.text
        assert logged, "the failure must still be logged — redaction is not silencing"
        assert url.rsplit("/", 1)[-1] not in logged, logged
