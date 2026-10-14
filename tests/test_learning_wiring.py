"""
tests/test_learning_wiring.py

Autofix outcomes are recorded once, from the PR's `closed` event, and shown
beside the next autofix as its track record.
"""

from unittest.mock import MagicMock, patch


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


class TestFixPromptHasNoInventedConventions:
    """The /fix prompt carried "Repo conventions (learned from previously
    accepted fixes)", fed by a store nothing ever wrote — its test mocked the
    reader to return data. The line and the store are gone."""

    def test_the_fix_prompt_claims_no_learned_conventions(self):
        from app.handlers.comments import generator

        captured = {}

        def _fake_ask(system, user, **kw):
            captured["user"] = user
            return ({"root_cause": "x", "fix": "y", "explanation": "z", "test": "t"}, MagicMock())

        with patch("app.ai.guarded.safe_router_ask", side_effect=_fake_ask):
            generator.cmd_fix("bug title", "some context")
        assert "conventions" not in captured["user"].lower()

    def test_the_store_is_gone(self):
        from app.core import learning

        for name in ("get_pattern_summary", "record_pattern", "get_acceptance_rate", "record_fix_accepted"):
            assert not hasattr(learning, name), name


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

    def test_an_unreadable_record_is_not_shown_as_none_merged(self):
        from app.handlers.autofix import _track_record

        with patch("app.core.learning.get_redis", side_effect=ConnectionError("down")):
            assert _track_record("o/r") == ""


class TestCountersAgainstRealRedis:
    """The counters themselves, against the in-memory Redis rather than a mock."""

    def test_merged_and_closed_are_counted_separately(self, fake_redis):
        from app.core import learning

        learning.record_autofix_merged("o/r", 1, 2)
        learning.record_autofix_merged("o/r", 3, 4)
        learning.record_autofix_closed("o/r", 5)
        assert learning.get_learning_summary("o/r") == {"autofix_merged": 2, "autofix_closed": 1}
        assert learning.get_learning_summary("o/other") == {"autofix_merged": 0, "autofix_closed": 0}

    def test_summary_raises_when_redis_is_down(self):
        import pytest

        from app.core import learning

        with patch("app.core.learning.get_redis", side_effect=ConnectionError("down")), \
             pytest.raises(ConnectionError):
            learning.get_learning_summary("o/r")
