"""
Retry safety for writes.

A 502/503/504 from GitHub's edge means the gateway could not return an
answer. It does NOT mean GitHub failed to act. The session used to replay
POST, PUT, PATCH and DELETE on those, so a response lost on the way back
duplicated whatever the call had already done — and this bot posts comments
on every push.
"""

import contextlib
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.github.client import IDEMPOTENT_METHODS, _session


class TestRetryPolicy:
    def test_only_idempotent_methods_are_replayed_on_5xx(self):
        retry = _session.get_adapter("https://api.github.com").max_retries
        for method in ("GET", "HEAD", "OPTIONS"):
            assert retry.is_retry(method, 502), f"{method} should be retried"
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert not retry.is_retry(method, 502), (
                f"{method} must never be replayed on a 5xx — the write may "
                f"already have taken effect"
            )

    def test_writes_are_absent_from_the_allowed_set(self):
        assert frozenset({"GET", "HEAD", "OPTIONS"}) == IDEMPOTENT_METHODS
        assert not IDEMPOTENT_METHODS & {"POST", "PUT", "PATCH", "DELETE"}

    def test_client_errors_are_never_retried(self):
        retry = _session.get_adapter("https://api.github.com").max_retries
        for status in (400, 401, 403, 404, 422, 429):
            assert not retry.is_retry("GET", status), f"{status} must not be retried"


class _CountingHandler(BaseHTTPRequestHandler):
    """Counts requests per method and always answers 502."""

    counts: dict = {}

    def _respond(self):
        _CountingHandler.counts[self.command] = (
            _CountingHandler.counts.get(self.command, 0) + 1
        )
        self.send_response(502)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = _respond
    do_POST = _respond

    def log_message(self, *args):
        pass


@pytest.fixture
def failing_server():
    _CountingHandler.counts = {}
    server = HTTPServer(("127.0.0.1", 0), _CountingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", _CountingHandler
    server.shutdown()
    server.server_close()


class TestAgainstARealFailingServer:
    """
    The policy assertions above read configuration. These drive an actual
    socket, because a retry policy that is configured correctly and applied
    to the wrong adapter is still a duplicated write.
    """

    def test_a_post_reaches_the_server_exactly_once(self, failing_server):
        url, handler = failing_server
        # Retries exhausted or a 502 returned — either way, the count is
        # what this asserts on.
        with contextlib.suppress(Exception):
            _session.post(f"{url}/repos/o/r/issues/1/comments", json={"body": "x"}, timeout=5)
        assert handler.counts.get("POST", 0) == 1, (
            f"POST hit the server {handler.counts.get('POST', 0)} times on a 502. "
            f"Each one would be another comment on the pull request."
        )

    def test_a_get_is_retried(self, failing_server):
        url, handler = failing_server
        with contextlib.suppress(Exception):
            _session.get(f"{url}/repos/o/r", timeout=5)
        assert handler.counts.get("GET", 0) > 1, (
            "GET should still be retried on 502 — that is the transient-failure "
            "recovery this session exists for"
        )
