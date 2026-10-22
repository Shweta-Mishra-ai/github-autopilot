"""
The gap report searched only the tests a PR changed, so functions with tests
elsewhere in the repository were reported as untested. Replayed on this
repository's PR #110: 6 of the 7 gaps the live bot published were functions
already tested elsewhere or with tests in the same PR.
"""

import base64
from unittest.mock import MagicMock, patch

from app.handlers.pull_request import gaps as G


class TestBodyChangesCountAsChangedSymbols:
    def test_an_edit_under_an_unchanged_def_names_that_function(self):
        patch_ = (
            "@@ -119,6 +119,6 @@ class LLMProvider(ABC):\n"
            " \n"
            " def _extract_json(text: str) -> dict:\n"
            '     """\n'
            "-    old\n"
            "+    new\n"
            '     """\n'
        )
        assert G.changed_symbols(patch_) == ["_extract_json"]

    def test_a_module_level_line_after_a_function_is_not_that_function(self):
        patch_ = "@@ -1,3 +1,4 @@\n def load_rows(x):\n     return x\n+print_banner()\n"
        assert "load_rows" not in G.changed_symbols(patch_)

    def test_the_method_above_a_hunk_is_found_from_the_head_file(self):
        source = "class P:\n    def call_raw(self):\n        a = 1\n        b = 2\n        return a\n"
        patch_ = "@@ -3,4 +3,3 @@ class P:\n         a = 1\n-        self._track(a)\n         b = 2\n"
        assert G._functions_at(source, G._touched_lines(patch_)) == ["call_raw"]


def _content(text):
    return {"content": base64.b64encode(text.encode()).decode(), "encoding": "base64"}


class TestRepositoryTestSearch:
    def test_a_test_file_naming_the_symbol_counts(self):
        def get(path, token):
            if path.startswith("/search/code"):
                return {"items": [{"path": "tests/test_x.py"}], "incomplete_results": False}
            return _content("def test_it():\n    assert _extract_json('{}') == {}\n")

        with patch("app.github.client.gh_get", side_effect=get):
            assert G._in_repository_tests("o/r", "t", "_extract_json") is True

    def test_a_tokenised_search_hit_without_the_word_does_not(self):
        def get(path, token):
            if path.startswith("/search/code"):
                return {"items": [{"path": "tests/test_x.py"}], "incomplete_results": False}
            return _content("extract the json from the answer\n")

        with patch("app.github.client.gh_get", side_effect=get):
            assert G._in_repository_tests("o/r", "t", "_extract_json") is False

    def test_a_failed_search_is_unknown_not_untested(self):
        with patch("app.github.client.gh_get", side_effect=RuntimeError("403")):
            assert G._in_repository_tests("o/r", "t", "load_rows") is None


class TestPublishedGaps:
    FILES = [
        {
            "filename": "app/a.py",
            "status": "modified",
            "patch": "@@ -1,2 +1,2 @@\n def load_rows(x):\n-    return 0\n+    return x\n",
        }
    ]

    def _run(self, gaps, tested):
        payload = {"has_gaps": True, "summary": "The test suite never reaches this.", "gaps": gaps}
        with (
            patch.object(G.router, "ask", return_value=(payload, MagicMock())),
            patch.object(G, "_in_repository_tests", return_value=tested),
        ):
            return G._detect_test_gaps({}, "o/r", 1, self.FILES, "t", MagicMock(footer=""), MagicMock())

    GAP = {"file": "app/a.py", "function": "load_rows", "risk": "high", "suggested_test": "cover x"}

    def test_a_function_tested_elsewhere_is_not_published(self):
        assert self._run([self.GAP], tested=True) == ""

    def test_an_untested_function_says_both_places_were_searched(self):
        out = self._run([self.GAP], tested=False)
        assert "no test in this PR or in the repository's existing tests mentions it" in out

    def test_an_unsearchable_repository_is_said_so(self):
        out = self._run([self.GAP], tested=None)
        assert "existing tests could not be searched" in out

    def test_a_function_the_pr_does_not_change_is_dropped(self):
        ghost = {**self.GAP, "function": "never_touched"}
        assert self._run([ghost], tested=False) == ""

    def test_the_models_claims_about_the_suite_are_not_published(self):
        out = self._run([self.GAP], tested=False)
        assert "never reaches" not in out


def test_one_search_per_symbol_and_one_read_per_file():
    """Round-4 performance audit: five gaps on one function searched five
    times and read the same test file five times (code search: 10/min)."""
    import base64
    from unittest.mock import MagicMock, patch

    from app.handlers.pull_request import gaps as G

    calls = []

    def gh(path, token):
        calls.append(path)
        if path.startswith("/search/code"):
            return {"items": [{"path": "tests/test_other.py"}], "incomplete_results": False}
        return {"content": base64.b64encode(b"def test_x(): unrelated()").decode()}

    gaps = [{"file": "app/x.py", "function": "do_work", "risk": "high"} for _ in range(5)]
    with patch("app.github.client.gh_get", side_effect=gh):
        kept = G._check_repository_tests(gaps, ["do_work"], "o/r", "t", MagicMock(), None)
    assert len(kept) == 5
    assert sum(p.startswith("/search/code") for p in calls) == 1
    assert sum("/contents/" in p for p in calls) == 1
