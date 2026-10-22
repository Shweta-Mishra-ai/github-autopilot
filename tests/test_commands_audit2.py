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
            try:
                pr_mod.handle(self._payload())
                self.raised = None
            except Exception as e:  # the transient ones are raised for the queue
                self.raised = e
        return upsert, update

    def test_providers_down_is_reported_not_swallowed(self):
        from app.ai.circuit_breaker import AllProvidersDown

        upsert, update = self._run(AllProvidersDown())
        body = upsert.call_args[0][4] if upsert.called else update.call_args[0][4]
        assert "Not run on this commit" in body and "code review" in body and "unavailable" in body
        # Written first, then raised: an outage passes, and the queue's retry
        # replaces the notice with the review. Returning ended the event.
        assert isinstance(self.raised, AllProvidersDown)

    def test_budget_refusal_is_reported(self):
        from app.core.guardrails import AIBudgetExceeded

        upsert, update = self._run(AIBudgetExceeded("Daily AI call limit (3) for this repository reached."))
        body = upsert.call_args[0][4] if upsert.called else update.call_args[0][4]
        assert "limit (3)" in body
        assert self.raised is None, "a spent daily budget is not retried within minutes"

    def test_unreadable_files_stop_the_analysis(self):
        from unittest.mock import patch

        from app.handlers import pull_request as pr_mod

        with patch("app.github.helpers.pr_files", side_effect=RuntimeError("502")), \
             patch.object(pr_mod, "get_installation_token", return_value="t"), \
             patch.object(pr_mod, "load_config") as cfg, \
             patch.object(pr_mod, "gh_get", return_value={}), \
             patch.object(pr_mod, "_analyze_pr") as analyze, \
             patch.object(pr_mod, "upsert_sticky") as upsert, \
             pytest.raises(RuntimeError, match="502"):
            cfg.return_value.pr_enabled.return_value = True
            pr_mod.handle(self._payload())
        analyze.assert_not_called()
        upsert.assert_not_called()


class TestRuntests:
    def _run(self, workflows, issue=None, pr=None):
        from unittest.mock import patch

        from app.handlers.comments import integrations as I

        def get(path, token):
            if path.endswith("/actions/workflows"):
                return {"workflows": workflows}
            if "/pulls/" in path:
                return pr or {}
            return {"default_branch": "main"}

        with patch.object(I, "gh_get", side_effect=get), patch.object(I, "gh_post") as post:
            out = I.cmd_runtests("o/r", 5, "tok", issue=issue)
        return out, post

    def test_a_release_workflow_is_never_the_tests(self):
        wfs = [{"id": 1, "name": "Release: build & publish to PyPI", "path": ".github/workflows/release.yml"},
               {"id": 2, "name": "CI", "path": ".github/workflows/ci.yml"}]
        _, post = self._run(wfs)
        assert post.call_args[0][0].endswith("/workflows/2/dispatches")

    def test_a_substring_is_not_a_match(self):
        wfs = [{"id": 1, "name": "Contest scoreboard", "path": ".github/workflows/contest.yml"}]
        out, post = self._run(wfs)
        post.assert_not_called()
        assert "No Test Workflow Found" in out

    def test_a_pr_runs_its_own_branch(self):
        wfs = [{"id": 2, "name": "CI", "path": ".github/workflows/ci.yml"}]
        pr = {"head": {"ref": "feature/x", "repo": {"full_name": "o/r"}}}
        out, post = self._run(wfs, issue={"pull_request": {}}, pr=pr)
        assert post.call_args[0][2] == {"ref": "feature/x"} and "feature/x" in out

    def test_a_fork_pr_says_it_ran_the_default_branch(self):
        wfs = [{"id": 2, "name": "CI", "path": ".github/workflows/ci.yml"}]
        pr = {"head": {"ref": "patch-1", "repo": {"full_name": "someone/fork"}}}
        out, post = self._run(wfs, issue={"pull_request": {}}, pr=pr)
        assert post.call_args[0][2] == {"ref": "main"} and "fork" in out


class TestReportRecordsWhatItShows:
    def test_a_merged_pr_is_recorded_with_its_time_to_merge(self):
        from unittest.mock import patch

        from app.handlers import pull_request

        payload = {"action": "closed", "repository": {"full_name": "o/r"}, "installation": {"id": 1},
                   "pull_request": {"number": 3, "merged": True, "head": {"ref": "feat"},
                                    "created_at": "2026-10-01T00:00:00Z", "merged_at": "2026-10-01T06:00:00Z"}}
        with patch("app.core.analytics.record_pr_merged") as rec:
            pull_request.handle(payload)
        rec.assert_called_once_with("o/r", 3, 6.0)

    def test_a_closed_issue_is_recorded(self):
        from unittest.mock import patch

        from app.handlers import issues

        payload = {"action": "closed", "repository": {"full_name": "o/r"}, "installation": {"id": 1},
                   "issue": {"number": 9, "created_at": "2026-10-01T00:00:00Z", "closed_at": "2026-10-02T00:00:00Z"}}
        with patch("app.core.analytics.record_issue_closed") as rec:
            issues.handle(payload)
        rec.assert_called_once_with("o/r", 9, 24.0)

    def test_an_empty_report_says_no_data_and_has_no_grade(self, monkeypatch):
        from app.core import analytics

        monkeypatch.setattr(analytics, "_get_int", lambda r, k: 0)
        monkeypatch.setattr(analytics, "_get_list", lambda r, k: [])
        monkeypatch.setattr(analytics, "_get_top_commands", lambda r, repo, days, top_n=5: {})
        assert analytics.format_report_comment("o/r") == ""
        monkeypatch.setattr(analytics, "_get_top_commands", lambda r, repo, days, top_n=5: {"fix": 2})
        out = analytics.format_report_comment("o/r")
        assert "Grade" not in out and "/fix" in out

    def test_the_iso_week_label(self, monkeypatch):
        from datetime import datetime, timezone

        from app.core import analytics

        class FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2024, 12, 30, 12, tzinfo=timezone.utc)

        monkeypatch.setattr(analytics, "datetime", FakeDT)
        assert analytics._week() == "2025-W01"


class TestSummarize:
    def test_a_deleted_account_does_not_fail_the_summary(self):
        from unittest.mock import MagicMock, patch

        from app.intelligence import summarizer

        comments = [{"user": None, "body": "first"}, {"user": {"login": "ana"}, "body": "second"}]
        with patch.object(summarizer.router, "ask_text", return_value=("summary", MagicMock())) as ask:
            assert summarizer.summarize_issue_thread(comments, {"title": "t"}) == "summary"
        assert "@?: first" in ask.call_args[0][1]

    def test_the_end_of_a_long_thread_reaches_the_model(self):
        from unittest.mock import MagicMock, patch

        from app.intelligence import summarizer

        comments = [{"user": {"login": f"u{i}"}, "body": "x" * 400} for i in range(49)]
        comments.append({"user": {"login": "maintainer"}, "body": "RESOLVED in v2.1"})
        with patch.object(summarizer.router, "ask_text", return_value=("s", MagicMock())) as ask:
            summarizer.summarize_issue_thread(comments, {"title": "t"})
        assert "RESOLVED in v2.1" in ask.call_args[0][1]
        assert "<THREAD>" in ask.call_args[0][1]

    def test_a_thread_too_long_for_everyone_keeps_the_opening_and_the_end(self):
        """When 120 characters each still overflow the prompt, the opening
        comment and the latest ones are kept and the gap is stated."""
        from unittest.mock import MagicMock, patch

        from app.intelligence import summarizer

        comments = [{"user": {"login": f"u{i}"}, "body": f"c{i}-" + "x" * 400} for i in range(200)]
        comments[-1]["body"] = "RESOLVED in v2.1"
        with patch.object(summarizer.router, "ask_text", return_value=("s", MagicMock())) as ask:
            summarizer.summarize_issue_thread(comments, {"title": "t"})
        prompt = ask.call_args[0][1]
        assert "c0-" in prompt and "RESOLVED in v2.1" in prompt
        assert "omitted for length" in prompt


class TestCiTakesTheLogBelowIt:
    """/ci's own help says to paste the log on the lines after `/ci`; the
    line-only argument rule silently dropped it."""

    def test_a_log_pasted_below_ci_is_its_argument(self):
        from app.handlers.comments.dispatcher import parse_command

        cmd, args = parse_command("/ci\n```\nE   ImportError: no module named foo\n```")
        assert cmd == "/ci"
        assert "ImportError: no module named foo" in args

    def test_other_commands_still_take_only_their_line(self):
        from app.handlers.comments.dispatcher import parse_command

        assert parse_command("/rollback 2\nconfirm later, maybe") == ("/rollback", "2")

    def test_the_end_of_a_long_log_is_kept(self):
        from app.handlers.comments.reviewer import _log_tail

        log = "setup\n" * 2000 + "FAILED test_x - AssertionError"
        out = _log_tail(log)
        assert out.endswith("FAILED test_x - AssertionError")
        assert "earlier characters omitted" in out


def test_denied_commands_are_not_counted_as_used():
    from unittest.mock import MagicMock, patch

    from app.handlers.comments import service

    payload = {
        "action": "created",
        "comment": {"body": "/merge", "user": {"login": "stranger", "type": "User"}, "id": 1},
        "sender": {"login": "stranger"},
        "issue": {"number": 3, "title": "t", "pull_request": {}},
        "repository": {"full_name": "o/r"},
        "installation": {"id": 1},
    }
    cfg = MagicMock()
    cfg.command_enabled.return_value = True
    with (
        patch.object(service, "get_installation_token", return_value="t"),
        patch.object(service, "load_config", return_value=cfg),
        patch.object(service, "check_user_rate_limit", return_value=True),
        patch.object(service, "check_command_permission", return_value=(False, "no")) as perm,
        patch.object(service, "_post_comment"),
        patch.object(service, "record_command_used") as rec,
    ):
        service.handle_comment_event(payload)
    assert perm.called, "the handler returned before the permission check"
    rec.assert_not_called()
