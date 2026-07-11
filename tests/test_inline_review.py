"""
tests/test_inline_review.py — V6.2 inline PR reviews.

Covers the pure diff-mapping helpers (app/github/patch_parser.py) and the
_review_code flow: inline Reviews-API path, suggestion-block safety rules,
and the fall-back-to-issue-comment guarantee.
"""

from unittest.mock import MagicMock, patch

from app.github.patch_parser import (
    commentable_lines,
    make_suggestion_block,
    nearest_commentable,
    parse_line_ref,
)

# A realistic unified diff: new-file line numbers 10-15.
# 10: context, 11: added, 12: added, 13: context, 14: context (blank), 15: context
PATCH = (
    "@@ -8,5 +10,6 @@ def handler():\n"
    "     token = get_token()\n"
    "+    if token is None:\n"
    "+        raise AuthError()\n"
    "-    old_line_removed()\n"
    "     do_work(token)\n"
    "\n"
    "     return ok\n"
)


class TestCommentableLines:

    def test_maps_added_and_context_lines(self):
        lines = commentable_lines(PATCH)
        assert set(lines) == {10, 11, 12, 13, 14, 15}
        assert lines[11] == ("    if token is None:", True)
        assert lines[12] == ("        raise AuthError()", True)
        assert lines[10] == ("    token = get_token()", False)

    def test_deleted_lines_not_commentable(self):
        contents = [c for c, _ in commentable_lines(PATCH).values()]
        assert not any("old_line_removed" in c for c in contents)

    def test_empty_or_headerless_patch(self):
        assert commentable_lines("") == {}
        assert commentable_lines("+def login(): pass") == {}  # no @@ hunk header

    def test_multiple_hunks(self):
        patch = (
            "@@ -1,2 +1,2 @@\n"
            "+first\n"
            " ctx\n"
            "@@ -10,2 +20,2 @@\n"
            "+second\n"
            " ctx2\n"
        )
        lines = commentable_lines(patch)
        assert lines[1] == ("first", True)
        assert lines[20] == ("second", True)


class TestParseLineRef:

    def test_variants(self):
        assert parse_line_ref(42) == 42
        assert parse_line_ref("42") == 42
        assert parse_line_ref("~42") == 42
        assert parse_line_ref("42-45") == 42
        assert parse_line_ref("around line 40") == 40
        assert parse_line_ref("?") is None
        assert parse_line_ref("") is None
        assert parse_line_ref(None) is None
        assert parse_line_ref(0) is None


class TestNearestCommentable:

    def test_exact_and_snap(self):
        lines = commentable_lines(PATCH)
        assert nearest_commentable(11, lines) == 11
        assert nearest_commentable(9, lines) == 10   # snaps up within distance
        assert nearest_commentable(50, lines) is None  # too far

    def test_prefers_added_over_context_on_tie(self):
        lines = commentable_lines(PATCH)
        # target 12: line 12 itself (added). target 13 is context; equidistant
        # 12(added) and 14(context) from target 13 → prefers... 13 is exact.
        # Construct a real tie: target between 11 (added) and 13 (context)? 12 is
        # added and exact. Use distance-1 tie: target 13 has exact match; use
        # synthetic map instead.
        synthetic = {5: ("ctx", False), 7: ("added", True)}
        assert nearest_commentable(6, synthetic) == 7  # tie → added wins

    def test_none_target(self):
        assert nearest_commentable(None, commentable_lines(PATCH)) is None


class TestSuggestionBlock:

    def test_single_line_fix_on_added_line(self):
        lines = commentable_lines(PATCH)
        block = make_suggestion_block("raise AuthError('no token')", 12, lines)
        assert block.startswith("```suggestion\n")
        # Indentation of the original line is preserved
        assert "        raise AuthError('no token')" in block

    def test_multiline_fix_refused(self):
        lines = commentable_lines(PATCH)
        assert make_suggestion_block("a\nb", 11, lines) == ""

    def test_context_line_refused(self):
        lines = commentable_lines(PATCH)
        assert make_suggestion_block("do_work(token, retry=True)", 13, lines) == ""

    def test_identical_content_refused(self):
        lines = commentable_lines(PATCH)
        assert make_suggestion_block("if token is None:", 11, lines) == ""

    def test_unknown_line_refused(self):
        assert make_suggestion_block("x = 1", 99, commentable_lines(PATCH)) == ""


# ── _review_code integration ──────────────────────────────────────────────────


def _cfg():
    cfg = MagicMock()
    cfg.get.side_effect = lambda *a, **kw: kw.get("default", True)
    cfg.footer = ""
    return cfg


def _review_with_issue(line_ref="11", fix="if token is None:  # check"):
    return {
        "score": 6,
        "summary": "Needs a guard",
        "issues": [
            {"severity": "major", "line": line_ref, "issue": "missing null check", "fix": fix}
        ],
        "confidence": 0.9,
    }


def _files():
    return [{"filename": "app/auth.py", "patch": PATCH, "additions": 2, "deletions": 1}]


def _run_review(review, post_mock):
    from app.ai.providers.base import LLMResponse

    meta = LLMResponse(text="ok", provider="groq_70b", model="llama", total_tokens=10)
    with patch("app.handlers.pull_request.router.ask", return_value=(review, meta)), \
         patch("app.handlers.pull_request.validate_code_review", return_value=review), \
         patch("app.handlers.pull_request.gh_post", post_mock):
        from app.handlers.pull_request import _review_code

        pr = {"head": {"sha": "abc1234"}}
        _review_code(pr, "org/repo", 1, _files(), "tok", _cfg(), MagicMock(), "", MagicMock())


class TestReviewCodeInline:

    def test_mappable_issue_posts_pull_review_with_line_comment(self):
        post = MagicMock()
        _run_review(_review_with_issue(), post)

        post.assert_called_once()
        path, _token, payload = post.call_args[0]
        assert path == "/repos/org/repo/pulls/1/reviews"
        assert payload["event"] == "COMMENT"
        assert payload["commit_id"] == "abc1234"
        [comment] = payload["comments"]
        assert comment["path"] == "app/auth.py"
        assert comment["line"] == 11
        assert comment["side"] == "RIGHT"
        assert "MAJOR" in comment["body"]
        assert "_fallback_md" not in comment  # internal key stripped before POST

    def test_unmappable_issue_stays_in_issue_comment(self):
        post = MagicMock()
        _run_review(_review_with_issue(line_ref="500"), post)  # far outside diff

        post.assert_called_once()
        path, _token, payload = post.call_args[0]
        assert path == "/repos/org/repo/issues/1/comments"  # classic path
        assert "missing null check" in payload["body"]

    def test_reviews_api_rejection_falls_back_with_findings(self):
        from app.github.client import GitHubError

        post = MagicMock(side_effect=[GitHubError("422 line not in diff", 422), {"id": 1}])
        _run_review(_review_with_issue(), post)

        assert post.call_count == 2
        second_path, _tok, second_payload = post.call_args_list[1][0]
        assert second_path == "/repos/org/repo/issues/1/comments"
        assert "### Findings" in second_payload["body"]
        assert "missing null check" in second_payload["body"]

    def test_committable_suggestion_for_single_line_fix_on_added_line(self):
        post = MagicMock()
        _run_review(_review_with_issue(fix="if token is None or token == '':"), post)

        [comment] = post.call_args[0][2]["comments"]
        assert "```suggestion" in comment["body"]
