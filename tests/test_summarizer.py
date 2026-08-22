"""
tests/test_summarizer.py

app/intelligence/summarizer.py sat at 0% coverage while being called from the
live PR path, because app/intelligence/* was excluded from the coverage source.

Both functions wrap everything in `except Exception -> return ""`, so any
malformed payload degrades to "no summary" with nothing surfaced to the user.
These tests pin the payload shapes GitHub actually sends — including the null
`user` on a deleted account and file entries missing `filename` — so that a
regression shows up as a failing test instead of a silently empty summary.
"""

from unittest.mock import patch

import pytest

from app.intelligence.summarizer import summarize_issue_thread, summarize_pr


@pytest.fixture
def ask_text():
    with patch("app.intelligence.summarizer.router.ask_text") as m:
        m.return_value = ("## 📋 What This PR Does\nStuff.", object())
        yield m


def _prompt(mock):
    """The user-role prompt passed to router.ask_text."""
    return mock.call_args[0][1]


class TestSummarizePr:
    def test_returns_model_text(self, ask_text):
        out = summarize_pr(pr={"title": "Add caching"}, files=[])
        assert out == "## 📋 What This PR Does\nStuff."

    def test_legacy_positional_signature_still_works(self, ask_text):
        out = summarize_pr(title="Legacy", body="b", files=[], repo="o/r")
        assert out
        assert "Legacy" in _prompt(ask_text)

    def test_no_arguments_does_not_raise(self, ask_text):
        assert summarize_pr() == "## 📋 What This PR Does\nStuff."

    def test_null_user_does_not_lose_the_summary(self, ask_text):
        """GitHub sends "user": null for a deleted account. .get("user", {})
        returns that None, and the chained .get() then raised AttributeError,
        which the broad except turned into an empty summary."""
        out = summarize_pr(pr={"title": "T", "user": None, "base": None, "head": None})
        assert out, "a null user must not wipe out the whole summary"

    def test_file_without_filename_does_not_lose_the_summary(self, ask_text):
        """f['filename'] raised KeyError on a partial file entry; every other
        read in this function used .get()."""
        out = summarize_pr(
            pr={"title": "T"},
            files=[{"additions": 3, "deletions": 1}],  # no 'filename'
        )
        assert out, "one malformed file entry must not wipe out the summary"

    def test_additions_and_deletions_are_totalled(self, ask_text):
        summarize_pr(
            pr={"title": "T"},
            files=[
                {"filename": "a.py", "additions": 10, "deletions": 2},
                {"filename": "b.py", "additions": 5, "deletions": 3},
            ],
        )
        assert "+15 -5" in _prompt(ask_text)

    def test_file_list_is_capped_at_ten(self, ask_text):
        files = [{"filename": f"f{i}.py"} for i in range(25)]
        summarize_pr(pr={"title": "T"}, files=files)
        prompt = _prompt(ask_text)
        assert "f9.py" in prompt
        assert "f10.py" not in prompt
        assert "25 files" in prompt

    def test_security_files_add_a_security_section(self, ask_text):
        summarize_pr(pr={"title": "T"}, files=[{"filename": "app/auth_token.py"}])
        assert "Security Review Needed" in _prompt(ask_text)

    def test_ordinary_files_do_not_add_a_security_section(self, ask_text):
        summarize_pr(pr={"title": "T"}, files=[{"filename": "app/views.py"}])
        assert "Security Review Needed" not in _prompt(ask_text)

    def test_dependency_file_adds_a_dependency_section(self, ask_text):
        summarize_pr(pr={"title": "T"}, files=[{"filename": "requirements.txt"}])
        assert "Dependency Changes" in _prompt(ask_text)

    def test_tests_present_vs_absent(self, ask_text):
        summarize_pr(pr={"title": "T"}, files=[{"filename": "tests/test_x.py"}])
        assert "Tests Included" in _prompt(ask_text)

        ask_text.reset_mock()
        summarize_pr(pr={"title": "T"}, files=[{"filename": "app/x.py"}])
        assert "No Tests Found" in _prompt(ask_text)

    def test_body_is_truncated(self, ask_text):
        summarize_pr(pr={"title": "T", "body": "x" * 5000})
        assert "x" * 801 not in _prompt(ask_text)

    def test_null_body_is_tolerated(self, ask_text):
        assert summarize_pr(pr={"title": "T", "body": None})

    def test_context_is_included_when_given(self, ask_text):
        summarize_pr(pr={"title": "T"}, context="uses redis for caching")
        assert "uses redis for caching" in _prompt(ask_text)

    def test_provider_failure_degrades_to_empty_string(self, ask_text):
        ask_text.side_effect = RuntimeError("all providers down")
        assert summarize_pr(pr={"title": "T"}) == ""


class TestSummarizeIssueThread:
    def test_returns_model_text(self, ask_text):
        ask_text.return_value = ("Summary text", object())
        assert summarize_issue_thread([], {"title": "Bug"}) == "Summary text"

    def test_thread_is_capped_at_twenty_comments(self, ask_text):
        comments = [
            {"user": {"login": f"u{i}"}, "body": f"comment-{i}"} for i in range(30)
        ]
        summarize_issue_thread(comments, {"title": "Bug"})
        prompt = _prompt(ask_text)
        assert "comment-19" in prompt
        assert "comment-25" not in prompt

    def test_missing_user_falls_back_to_placeholder(self, ask_text):
        summarize_issue_thread([{"body": "orphaned"}], {"title": "Bug"})
        assert "@?" in _prompt(ask_text)

    def test_provider_failure_degrades_to_empty_string(self, ask_text):
        ask_text.side_effect = RuntimeError("boom")
        assert summarize_issue_thread([], {"title": "Bug"}) == ""
