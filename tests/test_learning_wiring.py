"""
tests/test_learning_wiring.py — V6.2

The learning module (app/core/learning.py) existed unit-tested but UNWIRED for
three releases. These tests pin the actual wiring:

  record:  /apply (PR opened from a bot fix)  → record_fix_accepted
           /merge (bot autofix branch merged) → record_autofix_merged
  recall:  /fix prompt includes get_pattern_summary(repo) when non-empty
"""

from unittest.mock import MagicMock, patch


class TestApplyRecordsAcceptance:

    def _gh_get_side_effect(self, path, token):
        if path.startswith("/repos/o/r/branches/"):
            return {"name": "fix/bot-issue-42"}
        if path == "/repos/o/r":
            return {"default_branch": "main"}
        if path.startswith("/repos/o/r/pulls?"):
            return []
        raise AssertionError(f"unexpected gh_get {path}")

    def test_apply_records_fix_accepted(self):
        from app.handlers.comments import publisher

        with patch.object(publisher, "gh_get", side_effect=self._gh_get_side_effect), \
             patch.object(publisher, "gh_post", return_value={"number": 7, "title": "t", "html_url": "u"}), \
             patch("app.core.learning.record_fix_accepted") as rec:
            out = publisher.cmd_apply("o/r", 42, "tok", "fix/bot-issue-42")

        assert "PR Created" in out
        rec.assert_called_once_with("o/r", 42, "autofix")

    def test_apply_learning_failure_does_not_break_pr_creation(self):
        from app.handlers.comments import publisher

        with patch.object(publisher, "gh_get", side_effect=self._gh_get_side_effect), \
             patch.object(publisher, "gh_post", return_value={"number": 7, "title": "t", "html_url": "u"}), \
             patch("app.core.learning.record_fix_accepted", side_effect=RuntimeError("redis down")):
            out = publisher.cmd_apply("o/r", 42, "tok", "fix/bot-issue-42")

        assert "PR Created" in out  # learning is best-effort, never fatal


_NUMBERS = iter(range(10_000, 20_000))


def _closed(branch, merged, number=None, author="github-autopilot[bot]", head_repo="o/r"):
    # A fresh PR number per call unless one is given: each PR's outcome is
    # recorded once, so two tests sharing a number would see the second
    # deduplicated.
    return {
        "action": "closed",
        "pull_request": {
            "number": number if number is not None else next(_NUMBERS),
            "title": "fix: null deref",
            "merged": merged,
            "head": {"ref": branch, "repo": {"full_name": head_repo}},
            "user": {"login": author, "type": "Bot" if author.endswith("[bot]") else "User"},
        },
        "repository": {"full_name": "o/r"},
        "installation": {"id": 1},
    }


class TestAutofixOutcomesAreRecordedFromTheClosedEvent:
    """
    Recorded once, from the PR's `closed` event: a merge by any route, and —
    never recorded before — a rejection. /merge used to be the only writer,
    so a fix merged with GitHub's own button taught the bot nothing.
    """

    def test_a_merged_autofix_is_recorded_whoever_merged_it(self):
        from app.handlers import pull_request

        with patch("app.core.learning.record_autofix_merged") as merged, \
             patch("app.core.learning.record_autofix_closed") as closed:
            pull_request.handle(_closed("fix/bot-issue-42", merged=True, number=99))
        merged.assert_called_once_with("o/r", 99, 42)
        closed.assert_not_called()

    def test_a_rejected_autofix_is_recorded(self):
        from app.handlers import pull_request

        with patch("app.core.learning.record_autofix_merged") as merged, \
             patch("app.core.learning.record_autofix_closed") as closed, \
             patch("app.intelligence.memory.remember") as remember:
            pull_request.handle(_closed("fix/bot-issue-42", merged=False, number=98))
        closed.assert_called_once_with("o/r", 98)
        merged.assert_not_called()
        assert "rejected" in remember.call_args.args[1]

    def test_a_fork_pr_with_an_autofix_branch_name_records_nothing(self):
        """Anyone could fork, name a branch fix/bot-issue-1, close the PR and
        count a 'rejected' autofix — and plant its title in repo memory."""
        from app.handlers import pull_request

        with patch("app.core.learning.record_autofix_closed") as closed, \
             patch("app.intelligence.memory.remember") as remember:
            pull_request.handle(_closed("fix/bot-issue-1", merged=False, head_repo="mallory/r"))
        closed.assert_not_called()
        remember.assert_not_called()

    def test_a_same_repo_pr_not_opened_by_the_app_records_nothing(self):
        from app.handlers import pull_request

        with patch("app.core.learning.record_autofix_closed") as closed:
            pull_request.handle(_closed("fix/bot-issue-1", merged=False, author="alice"))
        closed.assert_not_called()

    def test_close_reopen_close_counts_once(self):
        from app.handlers import pull_request

        with patch("app.core.learning.record_autofix_closed") as closed:
            pull_request.handle(_closed("fix/bot-issue-7", merged=False, number=4242))
            pull_request.handle(_closed("fix/bot-issue-7", merged=False, number=4242))
        assert closed.call_count == 1

    def test_a_suffixed_autofix_branch_still_names_its_issue(self):
        from app.handlers import pull_request

        with patch("app.core.learning.record_autofix_merged") as merged:
            pull_request.handle(_closed("fix/bot-issue-42-1760000000", merged=True, number=97))
        merged.assert_called_once_with("o/r", 97, 42)

    def test_a_human_branch_records_nothing(self):
        from app.handlers import pull_request

        with patch("app.core.learning.record_autofix_merged") as merged, \
             patch("app.core.learning.record_autofix_closed") as closed:
            pull_request.handle(_closed("feat/human-work", merged=True, author="alice"))
        merged.assert_not_called()
        closed.assert_not_called()

    def test_merge_does_not_record_it_a_second_time(self):
        from app.handlers.comments import publisher

        pr = {"head": {"sha": "abc", "ref": "fix/bot-issue-42"}, "base": {"ref": "main"}}
        with patch.object(publisher, "gh_get", side_effect=[pr, [], {"total_count": 0, "check_runs": []}, {"statuses": []}]), \
             patch.object(publisher, "gh_put", return_value={"merged": True, "sha": "deadbeef1234"}), \
             patch.object(publisher, "gh_delete"), \
             patch("app.core.guardrails.check_pr_auto_merge", return_value=MagicMock(passed=True)), \
             patch("app.core.learning.record_autofix_merged") as rec:
            out = publisher.cmd_merge("o/r", 99, {"pull_request": {}}, "tok", "alice", MagicMock())
        assert "Merged" in out
        rec.assert_not_called()


class TestFixPromptRecallsPatterns:

    def _capture_fix_prompt(self, pattern_summary):
        from app.handlers.comments import generator

        captured = {}

        # V7: cmd_fix goes through app.ai.guarded.guarded_ask, which calls
        # app.ai.guarded.safe_router_ask. Patch there — patching
        # app.handlers.comments.router no longer intercepts and lets the call
        # reach the real provider.
        def _fake_ask(system, user, **kw):
            captured["user"] = user
            return (
                {
                    "root_cause": "x",
                    "fix": "y — a sufficiently long fix body",
                    "explanation": "z — why this works",
                    "test": "t",
                    "confidence": 0.9,
                },
                MagicMock(),
            )

        with patch("app.ai.guarded.safe_router_ask", side_effect=_fake_ask), \
             patch("app.core.learning.get_pattern_summary", return_value=pattern_summary):
            generator.cmd_fix("bug title", "some context", repo="o/r")
        return captured["user"]

    def test_learned_patterns_injected(self):
        prompt = self._capture_fix_prompt(" prefers-pathlib=True")
        assert "Repo conventions (learned from previously accepted fixes)" in prompt
        assert "prefers-pathlib=True" in prompt

    def test_no_patterns_no_noise(self):
        prompt = self._capture_fix_prompt("")
        assert "Repo conventions" not in prompt

    def test_no_repo_arg_skips_learning_entirely(self):
        from app.handlers.comments import generator

        with patch("app.ai.guarded.safe_router_ask",
                   return_value=({"root_cause": "x"}, MagicMock())), \
             patch("app.core.learning.get_pattern_summary") as gps:
            generator.cmd_fix("t", "c")  # no repo
        gps.assert_not_called()


class TestAutofixShowsItsTrackRecord:
    def test_shown_once_there_is_history(self):
        from app.handlers.autofix import _track_record

        with patch("app.core.learning.get_learning_summary",
                   return_value={"autofix_merged": 1, "autofix_closed": 4}):
            out = _track_record("o/r")
        assert "1 autofix PR(s) merged, 4 closed without merging" in out

    def test_silent_without_enough_history(self):
        from app.handlers.autofix import _track_record

        with patch("app.core.learning.get_learning_summary",
                   return_value={"autofix_merged": 1, "autofix_closed": 0}):
            assert _track_record("o/r") == ""
