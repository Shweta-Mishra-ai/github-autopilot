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


class TestMergeRecordsAutofixOutcome:

    def _run_merge(self, head_branch, record_mock):
        from app.handlers.comments import publisher

        pr = {"head": {"sha": "abc", "ref": head_branch}, "base": {"ref": "main"}}
        guard_ok = MagicMock(passed=True)

        with patch.object(publisher, "gh_get", side_effect=[pr, [], {"check_runs": []}]), \
             patch.object(publisher, "gh_put", return_value={"merged": True, "sha": "deadbeef1234"}), \
             patch.object(publisher, "gh_delete"), \
             patch("app.core.guardrails.check_pr_auto_merge", return_value=guard_ok), \
             patch("app.core.learning.record_autofix_merged", record_mock):
            return publisher.cmd_merge(
                "o/r", 99, {"pull_request": {}}, "tok", "alice", MagicMock()
            )

    def test_merge_of_bot_branch_records(self):
        rec = MagicMock()
        out = self._run_merge("fix/bot-issue-42", rec)
        assert "Merged" in out
        rec.assert_called_once_with("o/r", 99, 42)

    def test_merge_of_regular_branch_does_not_record(self):
        rec = MagicMock()
        out = self._run_merge("feat/human-work", rec)
        assert "Merged" in out
        rec.assert_not_called()


class TestFixPromptRecallsPatterns:

    def _capture_fix_prompt(self, pattern_summary):
        from app.handlers.comments import generator

        captured = {}

        def _fake_ask(system, user, **kw):
            captured["user"] = user
            return ({"root_cause": "x", "fix": "y", "explanation": "z", "test": "t",
                     "confidence": 0.9}, MagicMock())

        with patch("app.handlers.comments.router") as mock_router, \
             patch("app.core.learning.get_pattern_summary", return_value=pattern_summary):
            mock_router.ask.side_effect = _fake_ask
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

        with patch("app.handlers.comments.router") as mock_router, \
             patch("app.core.learning.get_pattern_summary") as gps:
            mock_router.ask.return_value = ({"root_cause": "x"}, MagicMock())
            generator.cmd_fix("t", "c")  # no repo
        gps.assert_not_called()
