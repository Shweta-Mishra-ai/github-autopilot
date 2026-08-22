"""
tests/test_retrieval.py

app/intelligence/retrieval.py sat at 17% coverage while feeding context into
the live PR-review and issue-triage prompts.

get_relevant_context() swallows everything, but get_context_for_pr() and
get_context_for_issue() build their query strings *outside* that guard — so a
payload shape GitHub sends routinely (null patch on a binary file, null body on
a description-less issue) raised TypeError straight out into the handler.
"""

from unittest.mock import patch

import pytest

from app.intelligence.retrieval import (
    MAX_CONTEXT_CHARS,
    get_context_for_issue,
    get_context_for_pr,
    get_relevant_context,
)


@pytest.fixture
def search():
    with patch("app.intelligence.embeddings.search_similar") as m:
        m.return_value = []
        yield m


class TestGetRelevantContext:
    def test_no_results_returns_empty(self, search):
        assert get_relevant_context("o/r", "query") == ""

    def test_formats_results_with_a_header(self, search):
        search.return_value = [
            {"filepath": "app/a.py", "content": "def a(): pass", "score": 0.9}
        ]
        out = get_relevant_context("o/r", "q")
        assert "## Relevant Codebase Context" in out
        assert "### app/a.py" in out
        assert "def a(): pass" in out

    def test_excluded_files_are_dropped(self, search):
        search.return_value = [
            {"filepath": "app/a.py", "content": "a", "score": 0.9},
            {"filepath": "app/b.py", "content": "b", "score": 0.9},
        ]
        out = get_relevant_context("o/r", "q", exclude_files=["app/a.py"])
        assert "app/a.py" not in out
        assert "app/b.py" in out

    def test_low_relevance_results_are_dropped(self, search):
        search.return_value = [
            {"filepath": "app/a.py", "content": "a", "score": 0.05}
        ]
        assert get_relevant_context("o/r", "q") == ""

    def test_everything_excluded_returns_empty_not_a_bare_header(self, search):
        search.return_value = [{"filepath": "app/a.py", "content": "a", "score": 0.9}]
        assert get_relevant_context("o/r", "q", exclude_files=["app/a.py"]) == ""

    def test_context_is_capped(self, search):
        search.return_value = [
            {"filepath": f"app/f{i}.py", "content": "x" * 800, "score": 0.9}
            for i in range(50)
        ]
        out = get_relevant_context("o/r", "q")
        assert len(out) < MAX_CONTEXT_CHARS + 200

    def test_falls_back_to_text_key(self, search):
        search.return_value = [{"filepath": "a.py", "text": "from-text", "score": 0.9}]
        assert "from-text" in get_relevant_context("o/r", "q")

    def test_backend_failure_degrades_silently(self, search):
        search.side_effect = RuntimeError("qdrant unreachable")
        assert get_relevant_context("o/r", "q") == ""


class TestGetContextForPr:
    def test_no_files_returns_empty_without_calling_backend(self, search):
        assert get_context_for_pr("o/r", []) == ""
        search.assert_not_called()

    def test_binary_file_with_null_patch_does_not_raise(self, search):
        """GitHub omits `patch` for binary files and oversized diffs, sending
        null. This function has no try/except, so None[:200] escaped into the
        review handler and took down the whole PR review."""
        files = [{"filename": "logo.png", "patch": None}]
        assert get_context_for_pr("o/r", files) == ""

    def test_mixed_binary_and_text_files_still_query(self, search):
        files = [
            {"filename": "logo.png", "patch": None},
            {"filename": "app/a.py", "patch": "@@ -1 +1 @@\n-a\n+b"},
        ]
        get_context_for_pr("o/r", files)
        query = search.call_args[0][1]
        assert "app/a.py" in query

    def test_null_filename_does_not_raise(self, search):
        assert get_context_for_pr("o/r", [{"filename": None, "patch": None}]) == ""

    def test_changed_files_are_excluded_from_their_own_context(self, search):
        search.return_value = [
            {"filepath": "app/a.py", "content": "self", "score": 0.9},
            {"filepath": "app/other.py", "content": "other", "score": 0.9},
        ]
        out = get_context_for_pr("o/r", [{"filename": "app/a.py", "patch": "x"}])
        assert "app/a.py" not in out
        assert "app/other.py" in out

    def test_only_first_five_files_form_the_query(self, search):
        files = [{"filename": f"f{i}.py", "patch": "p"} for i in range(10)]
        get_context_for_pr("o/r", files)
        query = search.call_args[0][1]
        assert "f4.py" in query
        assert "f7.py" not in query


class TestGetContextForIssue:
    def test_null_body_does_not_raise(self, search):
        """An issue opened with no description has body=None."""
        assert get_context_for_issue("o/r", "Crash on start", None) == ""

    def test_null_title_does_not_raise(self, search):
        assert get_context_for_issue("o/r", None, "some body") == ""

    def test_title_and_body_both_reach_the_query(self, search):
        get_context_for_issue("o/r", "Login broken", "stack trace here")
        query = search.call_args[0][1]
        assert "Login broken" in query
        assert "stack trace here" in query

    def test_body_is_truncated(self, search):
        get_context_for_issue("o/r", "T", "y" * 5000)
        assert len(search.call_args[0][1]) < 400
