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

from app.intelligence.summarizer import summarize_issue_thread


@pytest.fixture
def ask_text():
    with patch("app.intelligence.summarizer.router.ask_text") as m:
        m.return_value = ("## 📋 What This PR Does\nStuff.", object())
        yield m


def _prompt(mock):
    """The user-role prompt passed to router.ask_text."""
    return mock.call_args[0][1]


class TestSummarizeIssueThread:
    def test_returns_model_text(self, ask_text):
        ask_text.return_value = ("Summary text", object())
        assert summarize_issue_thread([], {"title": "Bug"}) == "Summary text"

    def test_every_fetched_comment_is_represented(self, ask_text):
        """This pinned the old cap of 20 comments — which dropped the end of
        the thread, usually where the resolution is."""
        comments = [
            {"user": {"login": f"u{i}"}, "body": f"comment-{i}"} for i in range(30)
        ]
        summarize_issue_thread(comments, {"title": "Bug"})
        prompt = _prompt(ask_text)
        assert all(f"comment-{i}" in prompt for i in range(30))

    def test_missing_user_falls_back_to_placeholder(self, ask_text):
        summarize_issue_thread([{"body": "orphaned"}], {"title": "Bug"})
        assert "@?" in _prompt(ask_text)

    def test_provider_failure_degrades_to_empty_string(self, ask_text):
        ask_text.side_effect = RuntimeError("boom")
        assert summarize_issue_thread([], {"title": "Bug"}) == ""
