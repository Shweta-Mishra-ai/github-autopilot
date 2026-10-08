"""
Autofix defects found by the 2026-10-08 audit, each pinned here.
"""

import base64
from unittest.mock import MagicMock, patch

from app.handlers import autofix


def _file(content: str, **extra) -> dict:
    data = {
        "type": "file",
        "encoding": "base64",
        "size": len(content.encode()),
        "content": base64.b64encode(content.encode()).decode(),
        "sha": "filesha",
    }
    data.update(extra)
    return data


class TestPathsCannotEscapeTheBlocklistThroughTheURL:
    """`#` and `?` end a URL path: the file checked and the file written
    differed (`app/core/authorization.py#.py` wrote authorization.py)."""

    def test_url_metacharacters_are_refused(self):
        for p in (
            "app/core/authorization.py#.py",
            "Dockerfile?x=.py",
            "%2egithub/workflows/ci.py",
            "app/x.py\nmore",
        ):
            assert autofix.normalise_path(p) == "", p
            assert autofix._is_allowed(p) is False, p

    def test_paths_are_quoted_in_the_url(self):
        assert autofix._contents_url("o/r", "docs/my notes.md") == "/repos/o/r/contents/docs/my%20notes.md"


class TestEditsApplyToTheOriginalFile:
    """The model returned the whole file and it was committed as given —
    including text the prompt sanitiser had rewritten."""

    ORIGINAL = (
        "Once installed, you are now ready to deploy.\n"
        "Use the ﬁle picker and the ™ mark.\n"
        "Teh end.\n"
    )

    def _apply(self, edits):
        with patch.object(autofix.router, "ask", return_value=({"edits": edits}, MagicMock(total_tokens=1))):
            fixed, _ = autofix._apply_fix(self.ORIGINAL, {"patch": "typo", "problem": "typo"}, "typo")
        return fixed

    def test_text_outside_the_edit_is_byte_for_byte_unchanged(self):
        fixed = self._apply([{"find": "Teh end.", "replace": "The end."}])
        assert fixed == self.ORIGINAL.replace("Teh end.", "The end.")
        assert "you are now" in fixed and "ﬁ" in fixed and "™" in fixed

    def test_an_edit_quoting_sanitised_text_is_refused(self):
        fixed = self._apply([{"find": "[ROLE_INJ] ready to deploy.", "replace": "x"}])
        assert fixed == self.ORIGINAL

    def test_an_ambiguous_find_is_refused(self):
        assert autofix._apply_edits("a = 1\na = 1\n", [{"find": "a = 1", "replace": "a = 2"}]) is None

    def test_overlapping_edits_are_refused(self):
        edits = [{"find": "abc", "replace": "x"}, {"find": "bcd", "replace": "y"}]
        assert autofix._apply_edits("abcd", edits) is None

    def test_several_edits_apply(self):
        out = autofix._apply_edits("a\nb\nc\n", [{"find": "a\n", "replace": "A\n"}, {"find": "c\n", "replace": "C\n"}])
        assert out == "A\nb\nC\n"

    def test_too_many_edits_are_refused(self):
        edits = [{"find": f"line{i}", "replace": "x"} for i in range(11)]
        assert autofix._apply_edits("\n".join(f"line{i}" for i in range(11)), edits) is None


class TestLargeFilesAreNeverReplaced:
    """Files of 1-100 MB come back with content "" and encoding "none"; that
    decoded to "", passed the size check, and was overwritten."""

    def test_a_file_github_does_not_return_inline_is_refused(self):
        big = {"type": "file", "encoding": "none", "content": "", "size": 2_400_000, "sha": "s"}
        with patch.object(autofix, "gh_get", return_value=big):
            target, content, sha, error = autofix._resolve_and_read("o/r", "data/catalog.json", "t")
        assert target == "" and "does not return this file inline" in error

    def test_reported_size_is_checked_before_decoding(self):
        data = _file("x = 1\n", size=10_000_000)
        with patch.object(autofix, "gh_get", return_value=data):
            _, _, _, error = autofix._resolve_and_read("o/r", "app/x.py", "t")
        assert error


class TestThePreviewShowsEveryChange:
    def test_an_inserted_line_does_not_mark_everything_changed(self):
        original = "\n".join(f"line{i}" for i in range(100)) + "\n"
        lines = original.splitlines()
        lines.insert(0, "import os")
        lines[90] = "os.system(input())"
        fixed = "\n".join(lines) + "\n"
        preview = autofix._make_diff_preview(original, fixed, "app/x.py")
        assert "os.system(input())" in preview
        assert "+2 lines, -1 lines" in preview

    def test_hidden_lines_are_counted(self):
        original = "\n".join(f"a{i}" for i in range(200)) + "\n"
        fixed = "\n".join(f"b{i}" for i in range(200)) + "\n"
        preview = autofix._make_diff_preview(original, fixed, "f.py")
        assert "more changed line(s) not shown" in preview


class TestEveryRunGetsItsOwnBranch:
    """A 422 'already exists' was swallowed and the commit went onto the
    previous run's branch."""

    def test_branch_names_are_unique_and_a_creation_error_is_reported(self):
        from app.github.client import GitHubError

        plan = {"target_file": "app/x.py", "confidence": 0.9, "patch": "p", "problem": "p"}
        ask = [
            (plan, MagicMock(total_tokens=1)),
            ({"edits": [{"find": "x = 1", "replace": "x = 2"}]}, MagicMock(total_tokens=1)),
        ]
        with patch.object(autofix.router, "ask", side_effect=ask), \
             patch.object(autofix, "gh_get", side_effect=[_file("x = 1\n"), {"default_branch": "main"}]), \
             patch.object(autofix, "_create_branch", side_effect=GitHubError("Reference already exists")), \
             patch.object(autofix, "gh_put") as put:
            out = autofix.run_autofix("o/r", 5, {"title": "t", "body": "b"}, "tok")
        assert "Branch Error" in out
        put.assert_not_called()

    def test_the_branch_keeps_the_autofix_prefix_and_issue(self):
        plan = {"target_file": "app/x.py", "confidence": 0.9, "patch": "p", "problem": "p"}
        ask = [
            (plan, MagicMock(total_tokens=1)),
            ({"edits": [{"find": "x = 1", "replace": "x = 2"}]}, MagicMock(total_tokens=1)),
        ]
        with patch.object(autofix.router, "ask", side_effect=ask), \
             patch.object(autofix, "gh_get", side_effect=[_file("x = 1\n"), {"default_branch": "main"}]), \
             patch.object(autofix, "_create_branch") as create, \
             patch.object(autofix, "gh_put"):
            out = autofix.run_autofix("o/r", 5, {"title": "t", "body": "b"}, "tok")
        branch = create.call_args[0][2]
        assert branch.startswith("fix/bot-issue-5-")
        assert f"/apply {branch}" in out
        assert "/rollback" not in out, "/rollback cannot discard an autofix branch"


class TestConfidence:
    def test_percent_answers_are_read_as_percent(self):
        assert autofix._confidence({"confidence": 85}) == 0.85
        assert autofix._confidence({"confidence": "0.7"}) == 0.7
        assert autofix._confidence({"confidence": 850}) == 0.0
        assert autofix._confidence({"confidence": "high"}) == 0.0
