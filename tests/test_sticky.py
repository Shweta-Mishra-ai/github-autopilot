"""tests/test_sticky.py — one bot comment per thread, edited in place."""

from unittest.mock import patch

from app.github.sticky import MARKER_PR_REPORT, find_sticky, upsert_sticky


class TestFindSticky:
    def test_finds_comment_bearing_the_marker(self):
        comments = [
            {"id": 1, "body": "unrelated human comment"},
            {"id": 2, "body": f"## Report\n{MARKER_PR_REPORT}"},
        ]
        with patch("app.github.sticky.gh_get_all", return_value=comments):
            assert find_sticky("o/r", 5, "t", MARKER_PR_REPORT) == 2

    def test_returns_none_when_absent(self):
        with patch("app.github.sticky.gh_get_all", return_value=[{"id": 1, "body": "hi"}]):
            assert find_sticky("o/r", 5, "t", MARKER_PR_REPORT) is None

    def test_api_error_returns_none(self):
        with patch("app.github.sticky.gh_get_all", side_effect=Exception("boom")):
            assert find_sticky("o/r", 5, "t", MARKER_PR_REPORT) is None

    def test_handles_none_body(self):
        with patch("app.github.sticky.gh_get_all", return_value=[{"id": 1, "body": None}]):
            assert find_sticky("o/r", 5, "t", MARKER_PR_REPORT) is None


class TestUpsertSticky:
    def test_patches_when_sticky_exists(self):
        calls = {}
        with (
            patch("app.github.sticky.find_sticky", return_value=42),
            patch(
                "app.github.sticky.gh_patch",
                side_effect=lambda p, t, d: calls.update(patched=p),
            ),
            patch("app.github.sticky.gh_post") as post,
        ):
            upsert_sticky("o/r", 5, "t", MARKER_PR_REPORT, "body")
        assert "comments/42" in calls["patched"]
        post.assert_not_called()

    def test_posts_when_no_sticky(self):
        with (
            patch("app.github.sticky.find_sticky", return_value=None),
            patch("app.github.sticky.gh_patch") as patch_fn,
            patch("app.github.sticky.gh_post", return_value={"id": 9}) as post,
        ):
            upsert_sticky("o/r", 5, "t", MARKER_PR_REPORT, "body")
        post.assert_called_once()
        patch_fn.assert_not_called()

    def test_marker_is_appended_when_missing(self):
        sent = {}

        def _capture(p, t, d):
            sent["body"] = d["body"]
            return {"id": 1}

        with (
            patch("app.github.sticky.find_sticky", return_value=None),
            patch("app.github.sticky.gh_post", side_effect=_capture),
        ):
            upsert_sticky("o/r", 5, "t", MARKER_PR_REPORT, "no marker here")
        assert MARKER_PR_REPORT in sent["body"]

    def test_marker_not_duplicated_when_present(self):
        sent = {}

        def _capture(p, t, d):
            sent["body"] = d["body"]
            return {"id": 1}

        with (
            patch("app.github.sticky.find_sticky", return_value=None),
            patch("app.github.sticky.gh_post", side_effect=_capture),
        ):
            upsert_sticky("o/r", 5, "t", MARKER_PR_REPORT, f"body {MARKER_PR_REPORT}")
        assert sent["body"].count(MARKER_PR_REPORT) == 1

    def test_falls_back_to_post_when_patch_fails(self):
        """A deleted sticky must not lose the report."""
        with (
            patch("app.github.sticky.find_sticky", return_value=42),
            patch("app.github.sticky.gh_patch", side_effect=Exception("404")),
            patch("app.github.sticky.gh_post", return_value={"id": 9}) as post,
        ):
            upsert_sticky("o/r", 5, "t", MARKER_PR_REPORT, "body")
        post.assert_called_once()
