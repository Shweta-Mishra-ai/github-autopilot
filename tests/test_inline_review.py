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
    """
    V7: _review_code makes ONE batched call for the whole PR, so the response
    is {"files": [...]} with a "file" key per entry rather than a bare
    single-file object.
    """
    return {
        "files": [
            {
                "file": "app/auth.py",
                "score": 6,
                "summary": "Needs a guard",
                "issues": [
                    {
                        "severity": "major",
                        "line": line_ref,
                        "issue": "missing null check",
                        "fix": fix,
                    }
                ],
            }
        ],
        "confidence": 0.9,
    }


def _files():
    return [{"filename": "app/auth.py", "patch": PATCH, "additions": 2, "deletions": 1}]


def _run_review(review, post_mock, sticky_mock=None):
    """
    Drive the V7 flow: _review_code builds, _post_inline_review posts.

    Before V7 _review_code posted directly; the split lets handle() consolidate
    the conversation-tab output into one sticky comment while line-anchored
    findings keep going through the Reviews API. Returns the review markdown
    so tests can assert on what lands in the sticky body.
    """
    from app.ai.providers.base import LLMResponse

    meta = LLMResponse(text="ok", provider="groq_70b", model="llama", total_tokens=10)
    cfg = _cfg()
    pr = {"head": {"sha": "abc1234"}}

    # The real validator now runs per-entry — patching it out would hide the
    # summary/verdict contract this suite is meant to protect.
    with patch("app.handlers.pull_request.router.ask", return_value=(review, meta)), \
         patch("app.handlers.pull_request.review.gh_post", post_mock), \
         patch("app.handlers.pull_request.upsert_sticky", sticky_mock or MagicMock()):
        from app.handlers.pull_request import _post_inline_review, _review_code

        md, inline = _review_code(
            pr, "org/repo", 1, _files(), "tok", cfg, MagicMock(), "", MagicMock()
        )
        if inline:
            # Fold the recovery back in, exactly as handle() does. The harness
            # used to drop this return value — the same mistake production was
            # making — which is why the rejection test below passed while a 422
            # silently lost every anchored finding.
            unposted = _post_inline_review(
                pr, "org/repo", 1, "tok", cfg, inline, MagicMock()
            )
            if unposted:
                md = f"{md}\n\n{unposted}".strip()
        return md


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

    def test_unmappable_issue_stays_in_the_report_body(self):
        """A finding that maps to no diff line must still reach the reader."""
        post = MagicMock()
        md = _run_review(_review_with_issue(line_ref="500"), post)  # far outside diff

        post.assert_not_called()  # nothing anchored → no Reviews API call
        assert "missing null check" in md

    def test_reviews_api_rejection_does_not_lose_findings(self):
        """
        A 422 from the Reviews API must not lose the finding.

        The old docstring here said the finding "is already in the markdown",
        and that was wrong: a finding that anchors to a diff line is
        deliberately LEFT OUT of the per-file markdown, which renders
        "All findings posted as inline comments" in its place. So on a 422 the
        finding existed nowhere, and the sticky report asserted it had been
        posted as an inline comment that did not exist.

        _post_inline_review returns the recovery markdown for exactly this
        case. handle() dropped it; so did this test's harness.
        """
        from app.github.client import GitHubError

        post = MagicMock(side_effect=GitHubError("422 line not in diff", 422))
        md = _run_review(_review_with_issue(), post)

        assert post.call_count == 1
        assert "Needs a guard" in md
        assert "would not accept" in md, "the reader is not told why it is here"

    def test_a_successful_post_adds_no_fallback_noise(self):
        """The recovery block must appear only when something was rejected."""
        post = MagicMock()
        md = _run_review(_review_with_issue(), post)

        post.assert_called_once()
        assert "would not accept" not in md

    def test_the_caller_folds_the_recovery_into_the_report(self):
        """Pins the wiring, not just the return value — the function returned
        this markdown correctly the whole time and nobody read it."""
        import inspect

        from app.handlers import pull_request as pr_mod

        src = inspect.getsource(pr_mod.handle)
        assert "_post_inline_review(" in src
        assert "unposted_md" in src, "the return value is being discarded again"

    def test_committable_suggestion_for_single_line_fix_on_added_line(self):
        post = MagicMock()
        _run_review(_review_with_issue(fix="if token is None or token == '':"), post)

        [comment] = post.call_args[0][2]["comments"]
        assert "```suggestion" in comment["body"]
