"""
tests/test_transient_retry.py

The queue retries an event only when the handler raises a transient error.
The handlers caught those errors themselves and returned, so the outcome was
OK and the event was gone — GitHub had been answered 202 and never redelivers:

  * an installation-token timeout: the PR was never reviewed, the command
    never answered, the issue never triaged;
  * a 502 on the PR's file list: the PR was never reviewed;
  * a 5xx or secondary rate limit on a command's reply: the answer was lost.

A retry must not repeat what already happened, so a reply is retried on its
own (the command is not run again) and the notifications a PR event sends are
sent once.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from app.github.client import GitHubError


def _pr_payload(action="opened"):
    return {
        "action": action,
        "pull_request": {
            "number": 7, "title": "t", "body": "", "user": {"login": "dev"},
            "head": {"sha": "abc", "ref": "f"}, "base": {"ref": "main"}, "draft": False,
        },
        "repository": {"full_name": "o/r"},
        "installation": {"id": 1},
        "sender": {"login": "dev"},
    }


class TestTheRunnerSeesTheFailure:
    """Driven through server._run_handler, which is what the queue calls."""

    def test_a_token_timeout_on_a_pr_is_retried(self):
        import requests

        import server

        with patch("app.handlers.pull_request.get_installation_token",
                   side_effect=requests.ConnectionError("unreachable")):
            assert server._run_handler("pull_request", _pr_payload(), "o/r") == "retry"

    def test_a_502_on_the_pr_files_is_retried(self):
        import server
        from app.core.config import Config

        with patch("app.handlers.pull_request.get_installation_token", return_value="t"), \
             patch("app.handlers.pull_request.load_config", return_value=Config({})), \
             patch("app.handlers.pull_request.gh_get", return_value={"archived": False}), \
             patch("app.github.helpers.pr_files", side_effect=GitHubError("bad gateway", 502)):
            assert server._run_handler("pull_request", _pr_payload(), "o/r") == "retry"

    def test_a_token_timeout_on_an_issue_is_retried(self):
        import requests

        import server

        payload = {"action": "opened", "issue": {"number": 3, "title": "t", "body": "",
                                                 "user": {"login": "dev"}, "labels": []},
                   "repository": {"full_name": "o/r"}, "installation": {"id": 1},
                   "sender": {"login": "dev"}}
        with patch("app.handlers.issues.get_installation_token",
                   side_effect=requests.Timeout("slow")):
            assert server._run_handler("issues", payload, "o/r") == "retry"


class TestTheReportIsRetriedAfterAFailedUpsert:
    def _run(self, upsert_error, review=("", [])):
        from app.handlers import pull_request as pr_mod

        cfg = MagicMock()
        cfg.pr_enabled.return_value = True
        cfg.get.side_effect = lambda *a, **k: k.get("default", True)
        cfg.footer = ""
        with patch.object(pr_mod, "get_installation_token", return_value="t"), \
             patch.object(pr_mod, "load_config", return_value=cfg), \
             patch.object(pr_mod, "gh_get", return_value={}), \
             patch("app.github.helpers.pr_files", return_value=[{"filename": "a.py", "patch": "+x"}]), \
             patch.object(pr_mod, "_analyze_pr", return_value="analysis"), \
             patch.object(pr_mod, "_build_pr_summary", return_value="summary"), \
             patch.object(pr_mod, "_review_code", return_value=review), \
             patch.object(pr_mod, "_detect_test_gaps", return_value=""), \
             patch.object(pr_mod, "notify_pr_opened"), \
             patch.object(pr_mod, "upsert_sticky", side_effect=upsert_error):
            pr_mod.handle(_pr_payload())

    def test_a_5xx_on_the_report_is_raised(self):
        with pytest.raises(GitHubError):
            self._run(GitHubError("server error", 503))

    def test_a_permanent_refusal_is_logged_not_retried(self):
        self._run(GitHubError("forbidden", 403))  # retrying a 403 cannot help


class TestARetryDoesNotRepeatItsSideEffects:
    def test_the_pr_opened_notification_is_sent_once(self, fake_redis):
        from app.handlers import pull_request as pr_mod

        with patch.object(pr_mod, "get_installation_token", return_value="t"), \
             patch.object(pr_mod, "load_config") as cfg, \
             patch.object(pr_mod, "gh_get", return_value={}), \
             patch("app.github.helpers.pr_files", return_value=[{"filename": "a.py", "patch": "+x"}]), \
             patch.object(pr_mod, "_analyze_pr", return_value=""), \
             patch.object(pr_mod, "_build_pr_summary", return_value=""), \
             patch.object(pr_mod, "_review_code", side_effect=GitHubError("bad gateway", 502)), \
             patch.object(pr_mod, "_detect_test_gaps", return_value=""), \
             patch.object(pr_mod, "upsert_sticky"), \
             patch.object(pr_mod, "notify_pr_opened") as notify:
            cfg.return_value.pr_enabled.return_value = True
            cfg.return_value.get.side_effect = lambda *a, **k: k.get("default", True)
            cfg.return_value.footer = ""
            for _ in range(3):  # the first run and two retries
                with pytest.raises(GitHubError):
                    pr_mod.handle(_pr_payload())
        assert notify.call_count == 1

    def test_once_is_true_only_for_the_first_caller(self, fake_redis):
        from app.core.transient import once

        assert once("k") is True
        assert once("k") is False
        assert once("other") is True

    def test_once_answers_true_when_redis_is_unreachable(self):
        from app.core.transient import once

        with patch("app.core.redis_client.get_redis", side_effect=ConnectionError("down")):
            assert once("k") is True, "a duplicate alert beats a missing one"


class TestAParkedReply:
    def test_it_is_scheduled_with_the_rate_limit_wait(self, fake_redis, monkeypatch):
        from app.core import event_queue as eq
        from app.github import deferred_reply
        from app.github.client import GitHubSecondaryRateLimitError

        monkeypatch.setattr(eq, "is_redis_available", lambda: True)
        post = MagicMock(side_effect=GitHubSecondaryRateLimitError(retry_after=90))
        deferred_reply.post_comment("o/r", 3, "tok", 11, "the answer", post=post)

        now = time.time()
        assert fake_redis.zrangebyscore(eq.DELAYED_KEY, 0, now + 60) == [], "waits the 90s asked"
        (raw,) = fake_redis.zrangebyscore(eq.DELAYED_KEY, 0, now + 120)
        env = json.loads(raw)
        assert env["event"] == "autopilot.reply"
        p = env["payload"]
        assert (p["repo"], p["number"], p["installation_id"]) == ("o/r", 3, 11)
        assert p["body"] == f"the answer\n{p['marker']}" and p["since"]

    def test_a_reply_that_did_reach_github_is_not_posted_twice(self):
        """Round-4 audit: a read timeout or 5xx on the POST means the answer
        was lost, not that the comment was not created. The deferred delivery
        posted it again, so "✅ Merged!" appeared twice."""
        from app.github import deferred_reply

        marker = "<!-- autopilot-reply:abc -->"
        payload = {"repo": "o/r", "number": 3, "installation_id": 11,
                   "body": f"the answer\n{marker}", "marker": marker, "since": "2026-01-01T00:00:00Z"}
        with patch("app.github.auth.get_installation_token", return_value="t"), \
             patch("app.github.client.gh_get", return_value=[{"body": f"the answer\n{marker}"}]) as get, \
             patch("app.github.deferred_reply.gh_post") as post:
            deferred_reply.deliver(payload)
        post.assert_not_called()
        assert "since=2026-01-01T00:00:00Z" in get.call_args[0][0]

        with patch("app.github.auth.get_installation_token", return_value="t"), \
             patch("app.github.client.gh_get", return_value=[{"body": "something else"}]), \
             patch("app.github.deferred_reply.gh_post") as post:
            deferred_reply.deliver(payload)
        post.assert_called_once()

    def test_unreadable_comments_retry_rather_than_risk_a_duplicate(self):
        import server

        payload = {"repo": "o/r", "number": 3, "installation_id": 11, "body": "x",
                   "marker": "<!-- autopilot-reply:abc -->", "since": ""}
        with patch("app.github.auth.get_installation_token", return_value="t"), \
             patch("app.github.client.gh_get", side_effect=GitHubError("bad gateway", 502)), \
             patch("app.github.deferred_reply.gh_post") as post:
            assert server._run_handler("autopilot.reply", payload, "o/r") == "retry"
        post.assert_not_called()

    def test_it_is_delivered_by_the_runner(self):
        import server

        with patch("app.github.auth.get_installation_token", return_value="fresh"), \
             patch("app.github.deferred_reply.gh_post") as post:
            out = server._run_handler(
                "autopilot.reply",
                {"repo": "o/r", "number": 3, "installation_id": 11, "body": "the answer"},
                "o/r",
            )
        assert out == "ok"
        post.assert_called_once_with("/repos/o/r/issues/3/comments", "fresh", {"body": "the answer"})

    def test_a_failed_delivery_is_retried(self):
        import server

        with patch("app.github.auth.get_installation_token", return_value="fresh"), \
             patch("app.github.deferred_reply.gh_post", side_effect=GitHubError("bad gateway", 502)):
            out = server._run_handler(
                "autopilot.reply",
                {"repo": "o/r", "number": 3, "installation_id": 11, "body": "x"},
                "o/r",
            )
        assert out == "retry"


class TestTheInternalEventCannotBeSentFromOutside:
    SECRET = "test-webhook-secret-32chars-abc!"

    def test_a_signed_webhook_naming_it_is_refused(self, monkeypatch):
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", self.SECRET)
        monkeypatch.setenv("EVENT_QUEUE_CONSUMERS", "0")
        import server

        body = json.dumps({"repo": "o/r", "number": 1, "installation_id": 1, "body": "spoof"}).encode()
        sig = "sha256=" + hmac.new(self.SECRET.encode(), body, hashlib.sha256).hexdigest()
        with patch.object(server, "_dispatch") as dispatch, \
             patch("app.core.event_queue.enqueue") as enq:
            resp = server.app.test_client().post("/webhook", data=body, headers={
                "X-Hub-Signature-256": sig, "X-GitHub-Event": "autopilot.reply",
                "X-GitHub-Delivery": "d-spoof", "Content-Type": "application/json",
            })
        assert resp.status_code == 400
        dispatch.assert_not_called()
        enq.assert_not_called()
