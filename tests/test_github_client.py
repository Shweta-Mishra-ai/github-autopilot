"""
tests/test_github_client.py

Every feature in this application reaches GitHub through app/github/client.py,
and it was the least-tested module in the tree at 48% — the HTTP verbs, the
status-code mapping and the retry policy were almost entirely uncovered.

That is the wrong module to leave uncovered. Its error mapping decides whether
a command retries, gives up, or reports the wrong thing; its retry policy
decides whether a lost response posts a comment twice; and it is the only place
an installation token is attached to an outbound request.

Nothing here touches the network — the session is patched, which is what the
autouse guard in conftest requires anyway.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.github import client
from app.github.client import (
    GITHUB_API,
    GitHubError,
    GitHubSecondaryRateLimitError,
    IDEMPOTENT_METHODS,
    gh_delete,
    gh_get,
    gh_get_all,
    gh_patch,
    gh_post,
    gh_put,
)


def _response(status=200, body=None, headers=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.text = text
    r.content = b"{}" if body is not None else b""
    r.json.return_value = body if body is not None else {}
    return r


@pytest.fixture(autouse=True)
def no_rate_limit_wait():
    """check_and_wait is covered by its own suite; here it would only add a
    patch to every single test."""
    with patch.object(client, "check_and_wait", lambda *a, **k: None):
        yield


@pytest.fixture
def sent():
    """Capture what the session was asked to send."""
    calls = []

    def record(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return _response(200, {"ok": True})

    with patch.object(client._session, "request", side_effect=record):
        yield calls


# ── The credential boundary ──────────────────────────────────────────────────


class TestTheTokenGoesOnlyToGitHub:
    """gh_get accepted any string starting with "http" as a complete URL and
    sent the Authorization header to it. No call site does that today — every
    one builds a literal "/repos/{repo}/..." — so this was a latent primitive
    rather than a live hole, and it is still the wrong default for a function
    that attaches a credential to every request."""

    @pytest.mark.parametrize(
        "hostile",
        [
            "https://evil.example.com/collect",
            "http://evil.example.com/collect",
            # The one a suffix check would wave through. Anyone can register it.
            "https://api.github.com.evil.example/collect",
            # Credentials in the URL do not change who answers.
            "https://api.github.com@evil.example/collect",
        ],
    )
    def test_an_absolute_url_off_github_is_refused(self, hostile, sent):
        with pytest.raises(GitHubError) as exc:
            gh_get(hostile, "ghs_secret")

        assert sent == [], "the request was sent before it was checked"
        assert "Refusing" in str(exc.value)

    def test_the_refusal_does_not_echo_the_token(self):
        """An error string ends up in logs and in posted comments."""
        with pytest.raises(GitHubError) as exc:
            gh_get("https://evil.example.com/x", "ghs_SUPER_SECRET")
        assert "ghs_SUPER_SECRET" not in str(exc.value)

    def test_an_absolute_github_url_is_allowed(self, sent):
        """GitHub's own pagination and *_url fields are absolute."""
        gh_get(f"{GITHUB_API}/repos/o/r/issues?page=2", "tok")
        assert sent[0]["url"] == f"{GITHUB_API}/repos/o/r/issues?page=2"

    def test_a_relative_path_is_prefixed(self, sent):
        gh_get("/repos/o/r", "tok")
        assert sent[0]["url"] == f"{GITHUB_API}/repos/o/r"

    def test_a_path_that_merely_starts_with_http_is_still_relative(self, sent):
        """"http" as a prefix is not a scheme. /repos/x/httpclient is a path."""
        gh_get("/repos/o/httpclient", "tok")
        assert sent[0]["url"] == f"{GITHUB_API}/repos/o/httpclient"

    @pytest.mark.parametrize(
        "verb,args",
        [
            (gh_post, ({"a": 1},)),
            (gh_put, ({"a": 1},)),
            (gh_patch, ({"a": 1},)),
            (gh_delete, ()),
        ],
    )
    def test_writes_are_checked_too(self, verb, args, sent):
        """The hole was only ever in gh_get, but a rule that covers one verb is
        a rule someone reopens on the next one."""
        with pytest.raises(GitHubError):
            verb("https://evil.example.com/x", "tok", *args)
        assert sent == []


class TestEveryRequestCarriesTheAuthHeader:
    @pytest.mark.parametrize(
        "verb,args",
        [
            (gh_get, ()),
            (gh_post, ({"a": 1},)),
            (gh_put, ({"a": 1},)),
            (gh_patch, ({"a": 1},)),
            (gh_delete, ()),
        ],
    )
    def test_headers_and_api_version(self, verb, args, sent):
        verb("/repos/o/r", "tok123", *args)
        headers = sent[0]["headers"]
        assert headers["Authorization"] == "Bearer tok123"
        assert headers["X-GitHub-Api-Version"] == "2022-11-28"

    @pytest.mark.parametrize(
        "verb,args,method",
        [
            (gh_get, (), "GET"),
            (gh_post, ({"a": 1},), "POST"),
            (gh_put, ({"a": 1},), "PUT"),
            (gh_patch, ({"a": 1},), "PATCH"),
            (gh_delete, (), "DELETE"),
        ],
    )
    def test_the_right_http_method_is_used(self, verb, args, method, sent):
        verb("/repos/o/r", "tok", *args)
        assert sent[0]["method"] == method

    @pytest.mark.parametrize("verb", [gh_post, gh_put, gh_patch])
    def test_writes_send_a_json_body(self, verb, sent):
        verb("/repos/o/r", "tok", {"title": "hello"})
        assert sent[0]["json"] == {"title": "hello"}

    @pytest.mark.parametrize("verb,args", [(gh_get, ()), (gh_delete, ())])
    def test_reads_send_no_body(self, verb, args, sent):
        verb("/repos/o/r", "tok", *args)
        assert "json" not in sent[0]

    @pytest.mark.parametrize(
        "verb,args",
        [
            (gh_get, ()),
            (gh_post, ({"a": 1},)),
            (gh_put, ({"a": 1},)),
            (gh_patch, ({"a": 1},)),
            (gh_delete, ()),
        ],
    )
    def test_every_verb_sets_a_timeout(self, verb, args, sent):
        """A request with no timeout can hold a pool worker forever, and the
        pool is what turns a flood into a 503 instead of an OOM."""
        verb("/repos/o/r", "tok", *args)
        assert sent[0]["timeout"] == client.DEFAULT_TIMEOUT


# ── Status code mapping ──────────────────────────────────────────────────────


class TestSuccessfulResponses:
    def test_200_returns_the_parsed_body(self):
        with patch.object(client._session, "request", return_value=_response(200, {"id": 7})):
            assert gh_get("/x", "t") == {"id": 7}

    def test_201_returns_the_parsed_body(self):
        with patch.object(client._session, "request", return_value=_response(201, {"id": 8})):
            assert gh_post("/x", "t", {}) == {"id": 8}

    def test_204_returns_an_empty_dict(self):
        """DELETE answers 204 with no body; json() would raise."""
        r = _response(204)
        r.json.side_effect = ValueError("no body")
        with patch.object(client._session, "request", return_value=r):
            assert gh_delete("/x", "t") == {}

    def test_200_with_an_empty_body_does_not_raise(self):
        r = _response(200)
        r.content = b""
        r.json.side_effect = ValueError("no body")
        with patch.object(client._session, "request", return_value=r):
            assert gh_get("/x", "t") == {}


class TestErrorMapping:
    def test_404_says_not_found_and_names_the_path(self):
        with patch.object(client._session, "request", return_value=_response(404)):
            with pytest.raises(GitHubError) as exc:
                gh_get("/repos/o/missing", "t")
        assert exc.value.status_code == 404
        assert "/repos/o/missing" in str(exc.value)

    def test_422_surfaces_githubs_own_message(self):
        """422 is the one a user can usually fix — a bad ref, a duplicate PR —
        so the message matters more than the code."""
        body = {"message": "Reference already exists"}
        with patch.object(client._session, "request", return_value=_response(422, body)):
            with pytest.raises(GitHubError) as exc:
                gh_post("/x", "t", {})
        assert exc.value.status_code == 422
        assert "Reference already exists" in str(exc.value)

    def test_422_with_an_unreadable_body_still_raises_422(self):
        r = _response(422)
        r.json.side_effect = ValueError("not json")
        with patch.object(client._session, "request", return_value=r):
            with pytest.raises(GitHubError) as exc:
                gh_post("/x", "t", {})
        assert exc.value.status_code == 422

    def test_500_raises_after_the_sessions_own_retries(self):
        with patch.object(client._session, "request", return_value=_response(503)):
            with pytest.raises(GitHubError) as exc:
                gh_get("/x", "t")
        assert exc.value.status_code == 503

    def test_an_unmapped_status_is_still_an_error_with_its_code(self):
        r = _response(418, text="teapot" * 100)
        with patch.object(client._session, "request", return_value=r):
            with pytest.raises(GitHubError) as exc:
                gh_get("/x", "t")
        assert exc.value.status_code == 418

    def test_an_unmapped_status_truncates_the_body(self):
        """A GitHub error page can be megabytes; it ends up in a log line."""
        r = _response(418, text="x" * 10_000)
        with patch.object(client._session, "request", return_value=r):
            with pytest.raises(GitHubError) as exc:
                gh_get("/x", "t")
        assert len(str(exc.value)) < 400

    def test_a_connection_error_becomes_a_githuberror(self):
        """Callers catch GitHubError. A requests exception escaping here
        reaches the webhook handler as an unhandled 500."""
        import requests

        with patch.object(
            client._session, "request", side_effect=requests.exceptions.ConnectionError("dns")
        ):
            with pytest.raises(GitHubError) as exc:
                gh_get("/x", "t")
        assert exc.value.status_code == 0


class TestRateLimitResponses:
    def test_429_reports_the_retry_after_delay(self):
        r = _response(429, headers={"Retry-After": "45"})
        with patch.object(client._session, "request", return_value=r):
            with pytest.raises(GitHubError) as exc:
                gh_get("/x", "t")
        assert exc.value.status_code == 429
        assert "45" in str(exc.value)

    def test_429_with_an_http_date_retry_after_does_not_raise_valueerror(self):
        """int() on the HTTP-date form raised ValueError, and ValueError
        escapes every caller — they all catch GitHubError."""
        r = _response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
        with patch.object(client._session, "request", return_value=r):
            with pytest.raises(GitHubError) as exc:
                gh_get("/x", "t")
        assert exc.value.status_code == 429

    def test_429_with_no_header_uses_a_default(self):
        with patch.object(client._session, "request", return_value=_response(429)):
            with pytest.raises(GitHubError) as exc:
                gh_get("/x", "t")
        assert exc.value.status_code == 429

    def test_a_secondary_rate_limit_is_its_own_exception(self):
        """It carries retry_after so the caller can requeue rather than sleep.
        This used to time.sleep(60) inside a shared pool worker."""
        body = {"message": "You have exceeded a secondary rate limit"}
        r = _response(403, body, headers={"Retry-After": "90"})
        with patch.object(client._session, "request", return_value=r):
            with pytest.raises(GitHubSecondaryRateLimitError) as exc:
                gh_post("/x", "t", {})
        assert exc.value.retry_after == 90

    def test_abuse_detection_is_treated_as_a_secondary_limit(self):
        body = {"message": "You have triggered an abuse detection mechanism"}
        with patch.object(client._session, "request", return_value=_response(403, body)):
            with pytest.raises(GitHubSecondaryRateLimitError):
                gh_post("/x", "t", {})

    def test_a_plain_403_is_not_a_secondary_limit(self):
        """A missing permission must not be retried as a throttle — it will
        never succeed, and the honest answer names the permission."""
        body = {"message": "Resource not accessible by integration"}
        with patch.object(client._session, "request", return_value=_response(403, body)):
            with pytest.raises(GitHubError) as exc:
                gh_post("/x", "t", {})
        assert not isinstance(exc.value, GitHubSecondaryRateLimitError)
        assert "Resource not accessible" in str(exc.value)

    def test_a_403_with_an_unreadable_body_still_raises_403(self):
        r = _response(403)
        r.json.side_effect = ValueError("html error page")
        with patch.object(client._session, "request", return_value=r):
            with pytest.raises(GitHubError) as exc:
                gh_get("/x", "t")
        assert exc.value.status_code == 403


# ── Retry policy ─────────────────────────────────────────────────────────────


class TestOnlyReadsAreReplayed:
    """A 502/503/504 means the gateway had no answer, NOT that GitHub failed to
    act. Replaying a POST whose response was lost posts the comment twice."""

    def test_writes_are_not_in_the_replay_set(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert method not in IDEMPOTENT_METHODS, (
                f"{method} would be replayed on a 5xx, which duplicates whatever "
                "it already did"
            )

    def test_reads_are(self):
        assert "GET" in IDEMPOTENT_METHODS

    def test_the_session_only_force_retries_gateway_statuses(self):
        adapter = client._session.get_adapter("https://api.github.com")
        forced = set(adapter.max_retries.status_forcelist)
        assert forced == {502, 503, 504}
        assert 429 not in forced, "429 is handled explicitly, with Retry-After"
        assert 404 not in forced


# ── Pagination ───────────────────────────────────────────────────────────────


class TestPagination:
    def _pages(self, *pages):
        responses = [_response(200, page) for page in pages]
        return patch.object(client._session, "request", side_effect=responses)

    def test_a_short_page_ends_the_walk(self):
        with self._pages([{"n": i} for i in range(3)]) as m:
            result = gh_get_all("/repos/o/r/issues", "t")
        assert len(result) == 3
        assert m.call_count == 1, "a short page is the end; asking again wastes quota"

    def test_full_pages_are_concatenated(self):
        with self._pages([{"n": i} for i in range(100)], [{"n": 100}]):
            assert len(gh_get_all("/repos/o/r/issues", "t")) == 101

    def test_an_empty_page_ends_the_walk(self):
        with self._pages([{"n": i} for i in range(100)], []):
            assert len(gh_get_all("/x", "t")) == 100

    def test_the_query_separator_is_chosen_correctly(self, sent):
        gh_get_all("/repos/o/r/issues?state=open", "t")
        assert "?state=open&page=1" in sent[0]["url"]

    def test_a_path_without_a_query_gets_one(self, sent):
        gh_get_all("/repos/o/r/issues", "t")
        assert "/issues?page=1" in sent[0]["url"]

    def test_a_non_list_response_is_returned_as_is(self):
        """Some endpoints answer with an object; paging it would be nonsense."""
        with self._pages({"total_count": 2, "items": []}):
            assert gh_get_all("/search/x", "t") == {"total_count": 2, "items": []}

    def test_an_error_mid_walk_keeps_what_was_collected(self):
        """Half an answer beats an exception for a report that is about to be
        rendered — but see the truncation warning below."""
        responses = [_response(200, [{"n": i} for i in range(100)]), _response(500)]
        with patch.object(client._session, "request", side_effect=responses):
            assert len(gh_get_all("/x", "t")) == 100

    def test_max_pages_is_respected(self):
        full = [_response(200, [{"n": i} for i in range(100)]) for _ in range(10)]
        with patch.object(client._session, "request", side_effect=full):
            assert len(gh_get_all("/x", "t", max_pages=3)) == 300

    def test_hitting_the_cap_is_logged(self, caplog):
        """It used to stop at 500 items in silence, indistinguishable from
        having reached the end — so a caller counting issues on a busy repo got
        a plausible-looking number that was simply wrong."""
        full = [_response(200, [{"n": i} for i in range(100)]) for _ in range(3)]
        with patch.object(client._session, "request", side_effect=full):
            with caplog.at_level("WARNING"):
                gh_get_all("/repos/o/busy/issues", "t", max_pages=3)

        assert any("truncated" in r.message for r in caplog.records), (
            "a truncated result must say so"
        )

    def test_reaching_the_real_end_is_not_logged_as_truncation(self):
        """A warning that fires on the ordinary case is one people learn to
        ignore, which costs more than not having it."""
        with self._pages([{"n": 1}]):
            with patch.object(client.log, "warning") as warn:
                gh_get_all("/x", "t")
        assert warn.call_count == 0
