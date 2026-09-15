"""tests/test_sticky.py — one bot comment per thread, edited in place."""

from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from app.github.sticky import MARKER_PR_REPORT, find_sticky, upsert_sticky


def _paged(total: int, marker_at: int | None, per_page: int = 100):
    """A fake comments endpoint, and a record of the pages it was asked for.

    Returns (side_effect, pages_requested). The query is parsed rather than
    string-split, because "page=" is also a substring of "per_page=" — a shortcut
    that silently made a measurement of this very function read 1 request when
    the real answer was 5.
    """
    pages: list[int] = []

    def fake_get(path, token):
        page = int(parse_qs(urlparse(path).query)["page"][0])
        pages.append(page)
        start = (page - 1) * per_page
        return [
            {"id": 1000 + i, "body": MARKER_PR_REPORT if i == marker_at else "human comment"}
            for i in range(start, min(start + per_page, total))
        ]

    return fake_get, pages


class TestFindSticky:
    def test_finds_comment_bearing_the_marker(self):
        comments = [
            {"id": 1, "body": "unrelated human comment"},
            {"id": 2, "body": f"## Report\n{MARKER_PR_REPORT}"},
        ]
        with patch("app.github.sticky.gh_get", return_value=comments):
            assert find_sticky("o/r", 5, "t", MARKER_PR_REPORT) == 2

    def test_returns_none_when_absent(self):
        with patch("app.github.sticky.gh_get", return_value=[{"id": 1, "body": "hi"}]):
            assert find_sticky("o/r", 5, "t", MARKER_PR_REPORT) is None

    def test_api_error_returns_none(self):
        with patch("app.github.sticky.gh_get", side_effect=Exception("boom")):
            assert find_sticky("o/r", 5, "t", MARKER_PR_REPORT) is None

    def test_handles_none_body(self):
        with patch("app.github.sticky.gh_get", return_value=[{"id": 1, "body": None}]):
            assert find_sticky("o/r", 5, "t", MARKER_PR_REPORT) is None

    def test_an_empty_thread_is_one_request(self):
        fake, pages = _paged(total=0, marker_at=None)
        with patch("app.github.sticky.gh_get", side_effect=fake):
            assert find_sticky("o/r", 5, "t", MARKER_PR_REPORT) is None
        assert pages == [1]


class TestFindStickyStopsLooking:
    """It used to fetch every page and only then scan, so a busy pull request
    cost five API requests to find a comment sitting on page one. That is the
    normal case: the sticky is posted when the PR opens, edited afterwards, and
    GitHub returns comments oldest first."""

    def test_a_marker_on_the_first_page_costs_one_request(self):
        fake, pages = _paged(total=450, marker_at=3)
        with patch("app.github.sticky.gh_get", side_effect=fake):
            assert find_sticky("o/r", 5, "t", MARKER_PR_REPORT) == 1003
        assert pages == [1], "paged past the answer"

    def test_a_marker_on_a_later_page_is_still_found(self):
        fake, pages = _paged(total=450, marker_at=240)
        with patch("app.github.sticky.gh_get", side_effect=fake):
            assert find_sticky("o/r", 5, "t", MARKER_PR_REPORT) == 1240
        assert pages == [1, 2, 3]

    def test_a_short_page_ends_the_search(self):
        """Fewer than per_page results is the end of the thread; asking for the
        next page spends quota to be told the same thing."""
        fake, pages = _paged(total=150, marker_at=None)
        with patch("app.github.sticky.gh_get", side_effect=fake):
            assert find_sticky("o/r", 5, "t", MARKER_PR_REPORT) is None
        assert pages == [1, 2]

    def test_the_search_is_bounded(self):
        """A thread long enough to exhaust this is one where posting a fresh
        comment is the honest outcome."""
        from app.github.sticky import MAX_COMMENT_PAGES

        fake, pages = _paged(total=100_000, marker_at=None)
        with patch("app.github.sticky.gh_get", side_effect=fake):
            assert find_sticky("o/r", 5, "t", MARKER_PR_REPORT) is None
        assert len(pages) == MAX_COMMENT_PAGES

    def test_a_non_list_response_does_not_crash(self):
        """GitHub answers an error body as an object; iterating it for .get
        would raise inside a function documented never to."""
        with patch("app.github.sticky.gh_get", return_value={"message": "Not Found"}):
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
