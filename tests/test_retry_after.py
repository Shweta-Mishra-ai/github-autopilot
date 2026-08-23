"""
Retry-After parsing.

The bug these cover: three call sites used a bare int() on the header.
RFC 9110 allows an HTTP-date, and Groq sends fractional seconds — both
raised ValueError. In the GitHub client's 429 branch that ValueError was
not caught at all, and callers only ever catch GitHubError.
"""

import time

import pytest

from app.core.retry_after import MAX_RETRY_AFTER, parse_retry_after


class TestParseRetryAfter:
    @pytest.mark.parametrize(
        "header,expected",
        [
            ("60", 60),
            ("0", 0),
            ("  45  ", 45),
        ],
    )
    def test_delay_seconds(self, header, expected):
        assert parse_retry_after(header, 30) == expected

    def test_fractional_seconds_round_up(self):
        # Groq's real header shape. int("7.66") raises; we must not wait less
        # than asked, so this rounds up rather than truncating.
        assert parse_retry_after("7.66", 30) == 8

    def test_http_date_in_the_past_is_zero(self):
        assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", 30) == 0

    def test_http_date_in_the_future(self):
        future = time.gmtime(time.time() + 120)
        header = time.strftime("%a, %d %b %Y %H:%M:%S GMT", future)
        assert 110 <= parse_retry_after(header, 30) <= 125

    @pytest.mark.parametrize("header", [None, "", "soon", "-5", "nan", "inf", "1e999"])
    def test_unusable_values_fall_back_to_default(self, header):
        assert parse_retry_after(header, 30) == 30

    def test_absurd_delay_is_clamped(self):
        assert parse_retry_after("99999999", 30) == MAX_RETRY_AFTER

    def test_never_raises_on_arbitrary_input(self):
        for value in (object(), [], {}, b"12", 0.5, True):
            assert isinstance(parse_retry_after(value, 30), int)


class _Resp:
    """Minimal response double — only what _handle_response touches."""

    def __init__(self, status_code, headers, payload=None):
        self.status_code = status_code
        self.headers = headers
        self._payload = payload or {}
        self.content = b"{}"
        self.text = "{}"

    def json(self):
        return self._payload


class TestClientRateLimitBranches:
    def test_429_with_http_date_raises_githuberror_not_valueerror(self):
        from app.github.client import GitHubError, _handle_response

        resp = _Resp(429, {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
        with pytest.raises(GitHubError) as exc:
            _handle_response(resp, "GET", "/repos/o/r")
        assert exc.value.status_code == 429

    def test_secondary_limit_with_fractional_header_keeps_its_type(self):
        # Before the fix the ValueError from int() was swallowed by the
        # branch's own except, downgrading a transient rate limit to a
        # permanent-looking 403 Forbidden and losing retry_after entirely.
        from app.github.client import GitHubSecondaryRateLimitError, _handle_response

        resp = _Resp(
            403,
            {"Retry-After": "12.5"},
            {"message": "You have exceeded a secondary rate limit"},
        )
        with pytest.raises(GitHubSecondaryRateLimitError) as exc:
            _handle_response(resp, "POST", "/repos/o/r/issues/1/comments")
        assert exc.value.retry_after == 13

    def test_secondary_limit_with_missing_header_uses_default(self):
        from app.github.client import GitHubSecondaryRateLimitError, _handle_response

        resp = _Resp(403, {}, {"message": "abuse detection triggered"})
        with pytest.raises(GitHubSecondaryRateLimitError) as exc:
            _handle_response(resp, "POST", "/repos/o/r/issues/1/comments")
        assert exc.value.retry_after == 60

    def test_plain_403_is_still_a_permission_error(self):
        from app.github.client import GitHubError, GitHubSecondaryRateLimitError, _handle_response

        resp = _Resp(403, {}, {"message": "Resource not accessible by integration"})
        with pytest.raises(GitHubError) as exc:
            _handle_response(resp, "GET", "/repos/o/r")
        assert not isinstance(exc.value, GitHubSecondaryRateLimitError)
        assert exc.value.status_code == 403
