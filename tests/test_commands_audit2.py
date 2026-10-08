"""
Comment-command defects found by the 2026-10-08 audit, each pinned here.
"""

import pytest

from app.handlers.comments.dispatcher import extract_command, parse_command


class TestTheFirstCommandIsTheOneRun:
    @pytest.mark.parametrize(
        "body,expected",
        [
            ("/fix\n/autofix", "/fix"),
            ("/merge\n/release", "/merge"),
            ("please look\n/autofix app/x.py\n/fix", "/autofix"),
        ],
    )
    def test_first_not_longest(self, body, expected):
        assert extract_command(body) == expected

    def test_a_longer_command_word_is_not_a_shorter_command(self):
        assert extract_command("/autofix") == "/autofix"
        assert extract_command("/fixture please") is None


class TestNotInstructions:
    @pytest.mark.parametrize(
        "body",
        [
            "Steps I ran locally:\n\n    /release",
            "Steps:\n\t/merge",
            "LGTM\n<!--\n/merge\n-->",
            "<!-- /merge -->",
            "> /merge\nquoted",
            "```\n/merge\n```",
            "Run `/merge` later",
        ],
    )
    def test_ignored(self, body):
        assert extract_command(body) is None

    def test_a_command_after_a_hidden_one_still_counts(self):
        assert parse_command("<!-- /merge -->\n/fix") == ("/fix", "")


class TestArgumentsComeFromTheCommandLine:
    def test_args_are_the_rest_of_the_line_that_issued_the_command(self):
        body = "Running /rollback 2 confirm is scary, so first list them:\n/rollback"
        assert parse_command(body) == ("/rollback", "")

    def test_prose_containing_the_command_text_does_not_feed_args(self):
        assert parse_command("Our /circleci job fails\n/ci") == ("/ci", "")

    def test_a_quote_reply_does_not_feed_args(self):
        body = "> To create a PR: reply `/apply fix/bot-issue-42`\n\n/apply fix/bot-issue-42"
        assert parse_command(body) == ("/apply", "fix/bot-issue-42")

    def test_args_stop_at_the_end_of_the_line(self):
        assert parse_command("/autofix app/x.py\nplease be careful") == ("/autofix", "app/x.py")

    def test_args_keep_their_case_and_unicode_does_not_shift_them(self):
        assert parse_command("İİ\n/rollback 10 confirm") == ("/rollback", "10 confirm")
        assert parse_command("/autofix App/Models.py") == ("/autofix", "App/Models.py")

    def test_mention_prefix(self):
        assert parse_command("@github-autopilot /fix the crash") == ("/fix", "the crash")


class TestTheDailyBudgetHoldsForEveryAICall:
    def _with_counter(self, monkeypatch, used):
        from app.core import guardrails

        class KV:
            def __init__(self):
                self.v = used

            def incr(self, key):
                self.v += 1
                return self.v

            def decr(self, key):
                self.v -= 1
                return self.v

            def expire(self, *a):
                return True

        kv = KV()
        monkeypatch.setenv("REPO_DAILY_AI_LIMIT", "3")
        monkeypatch.setattr("app.core.redis_client.get_redis", lambda: kv)
        return guardrails, kv

    def test_a_call_over_the_limit_is_refused_and_not_counted(self, monkeypatch):
        guardrails, kv = self._with_counter(monkeypatch, used=3)
        with guardrails.metered("o/r"), pytest.raises(guardrails.AIBudgetExceeded):
            guardrails.charge_ai_call()
        assert kv.v == 3
        assert "limit (3)" in guardrails.budget_refusal()
        guardrails.reset_budget_refusal()

    def test_a_call_within_the_limit_is_counted(self, monkeypatch):
        guardrails, kv = self._with_counter(monkeypatch, used=1)
        with guardrails.metered("o/r"):
            guardrails.charge_ai_call()
        assert kv.v == 2 and guardrails.budget_refusal() == ""

    def test_the_router_refuses_before_calling_a_provider(self, monkeypatch):
        from unittest.mock import patch

        from app.ai.router import router

        guardrails, _ = self._with_counter(monkeypatch, used=3)
        with guardrails.metered("o/r"), \
             patch.object(router, "_call_provider") as call, \
             pytest.raises(guardrails.AIBudgetExceeded):
            router.ask("s", "u", task="explain")
        call.assert_not_called()
        guardrails.reset_budget_refusal()

    def test_a_slash_command_over_budget_says_so(self, monkeypatch):
        from unittest.mock import MagicMock, patch

        from app.handlers.comments import service

        guardrails, _ = self._with_counter(monkeypatch, used=3)
        payload = {
            "action": "created",
            "comment": {"body": "/explain", "user": {"login": "alice"}},
            "issue": {"number": 1, "title": "t", "body": "b"},
            "repository": {"full_name": "o/r"},
            "installation": {"id": 1},
            "sender": {"login": "alice"},
        }
        posted = []
        cfg = MagicMock()
        cfg.command_enabled.return_value = True
        with guardrails.metered("o/r"), \
             patch.object(service, "get_installation_token", return_value="t"), \
             patch.object(service, "load_config", return_value=cfg), \
             patch.object(service, "check_user_rate_limit", return_value=True), \
             patch.object(service, "check_command_permission", return_value=(True, "")), \
             patch.object(service, "_post_comment", side_effect=lambda *a, **k: posted.append(a[3])):
            service.handle_comment_event(payload)
        assert posted and "AI Budget Reached" in posted[-1] and "limit (3)" in posted[-1]
        guardrails.reset_budget_refusal()


class TestAPRSectionThatFailsIsReported:
    """AllProvidersDown (or the budget running out) in the review escaped
    handle(): nothing was written and the old report stayed up as current."""

    def _payload(self):
        return {
            "action": "synchronize",
            "pull_request": {"number": 3, "title": "t", "body": "", "head": {"sha": "abc", "ref": "f"},
                             "base": {"ref": "main"}, "user": {"login": "alice"}, "draft": False},
            "repository": {"full_name": "o/r"},
            "installation": {"id": 1},
            "sender": {"login": "alice"},
        }

    def _run(self, review_side_effect, files=None):
        from unittest.mock import MagicMock, patch

        from app.handlers import pull_request as pr_mod

        cfg = MagicMock()
        cfg.pr_enabled.return_value = True
        cfg.get.side_effect = lambda *a, **k: k.get("default", True)
        cfg.footer = ""
        with patch.object(pr_mod, "get_installation_token", return_value="t"), \
             patch.object(pr_mod, "load_config", return_value=cfg), \
             patch.object(pr_mod, "gh_get", return_value={}), \
             patch("app.github.helpers.pr_files", return_value=files if files is not None else [{"filename": "a.py", "patch": "+x"}]), \
             patch.object(pr_mod, "_analyze_pr", return_value="analysis"), \
             patch.object(pr_mod, "_build_pr_summary", return_value="summary"), \
             patch.object(pr_mod, "_review_code", side_effect=review_side_effect), \
             patch.object(pr_mod, "_detect_test_gaps", return_value=""), \
             patch.object(pr_mod, "upsert_sticky") as upsert, \
             patch.object(pr_mod, "update_sticky_if_present") as update:
            pr_mod.handle(self._payload())
        return upsert, update

    def test_providers_down_is_reported_not_swallowed(self):
        from app.ai.circuit_breaker import AllProvidersDown

        upsert, update = self._run(AllProvidersDown())
        body = upsert.call_args[0][4] if upsert.called else update.call_args[0][4]
        assert "Not run on this commit" in body and "code review" in body and "unavailable" in body

    def test_budget_refusal_is_reported(self):
        from app.core.guardrails import AIBudgetExceeded

        upsert, update = self._run(AIBudgetExceeded("Daily AI call limit (3) for this repository reached."))
        body = upsert.call_args[0][4] if upsert.called else update.call_args[0][4]
        assert "limit (3)" in body

    def test_unreadable_files_stop_the_analysis(self):
        from unittest.mock import patch

        from app.handlers import pull_request as pr_mod

        with patch("app.github.helpers.pr_files", side_effect=RuntimeError("502")), \
             patch.object(pr_mod, "get_installation_token", return_value="t"), \
             patch.object(pr_mod, "load_config") as cfg, \
             patch.object(pr_mod, "gh_get", return_value={}), \
             patch.object(pr_mod, "_analyze_pr") as analyze, \
             patch.object(pr_mod, "upsert_sticky") as upsert:
            cfg.return_value.pr_enabled.return_value = True
            pr_mod.handle(self._payload())
        analyze.assert_not_called()
        upsert.assert_not_called()
