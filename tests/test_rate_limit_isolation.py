"""
tests/test_rate_limit_isolation.py

Two production defects in app/github/rate_limit.py and app/github/auth.py, and
the properties that keep them fixed.

Neither was caught by anything: both modules sat at under 50% coverage, and
both bugs only show up with more than one installation or more than one thread
— which is exactly the shape of a real deployment and nothing like the shape of
the existing unit tests.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from app.github import rate_limit as rl


@pytest.fixture(autouse=True)
def clean_state():
    rl.reset_state()
    yield
    rl.reset_state()


def _headers(remaining, reset_at=None, resource="core"):
    h = {"X-RateLimit-Remaining": str(remaining), "X-RateLimit-Resource": resource}
    if reset_at is not None:
        h["X-RateLimit-Reset"] = str(int(reset_at))
    return h


# ── One installation must not speak for another ──────────────────────────────


class TestRateLimitIsPerInstallation:
    """
    GitHub meters per token. This module kept ONE global counter that every
    response overwrote, so on a multi-tenant deployment the number belonged to
    whichever installation answered last.

    Both directions of that are harmful: a busy tenant made an idle one look
    exhausted and get its calls refused, and an idle tenant masked a real
    exhaustion so `/health` reported fine while commands failed.
    """

    def test_two_installations_keep_separate_budgets(self):
        rl.update_from_headers(_headers(12), token="tenant-a")
        rl.update_from_headers(_headers(4800), token="tenant-b")

        assert rl.get_status("tenant-a")["remaining"] == 12
        assert rl.get_status("tenant-b")["remaining"] == 4800

    def test_a_busy_tenant_does_not_throttle_a_healthy_one(self):
        """The failure that mattered: B's request refused because A is spent."""
        rl.update_from_headers(_headers(1, reset_at=time.time() + 3600), token="tenant-a")
        rl.update_from_headers(_headers(4900, reset_at=time.time() + 3600), token="tenant-b")

        with pytest.raises(RuntimeError):
            rl.check_and_wait("tenant-a")

        rl.check_and_wait("tenant-b")  # must not raise, must not sleep

    def test_a_healthy_tenant_does_not_mask_an_exhausted_one(self):
        """Reported with no token, the answer must be the worst case — an
        average or a last-writer-wins lets a real exhaustion hide."""
        rl.update_from_headers(_headers(4900), token="tenant-healthy")
        rl.update_from_headers(_headers(3), token="tenant-spent")

        overall = rl.get_status()
        assert overall["remaining"] == 3
        assert overall["low"] is True

    def test_the_breakdown_never_contains_a_token(self):
        rl.update_from_headers(_headers(100), token="ghs_supersecrettokenvalue")
        status = rl.get_status()
        assert "ghs_supersecrettokenvalue" not in str(status)
        assert len(status["installations"]) == 1

    def test_the_same_token_maps_to_one_bucket(self):
        rl.update_from_headers(_headers(500), token="same")
        rl.update_from_headers(_headers(400), token="same")
        assert len(rl.get_status()["installations"]) == 1
        assert rl.get_status("same")["remaining"] == 400

    def test_tracking_is_bounded(self):
        """The key derives from the token, and tokens rotate hourly. Unbounded
        growth would be a slow leak in a long-lived process."""
        for i in range(rl.MAX_TRACKED + 40):
            rl.update_from_headers(_headers(100 + i), token=f"token-{i}")
        assert len(rl.get_status()["installations"]) <= rl.MAX_TRACKED

    def test_a_call_with_no_token_still_works(self):
        """Backward compatibility: the old signature took headers only."""
        rl.update_from_headers(_headers(77))
        assert rl.get_status()["remaining"] == 77


# ── Nothing here may freeze the thread pool ──────────────────────────────────


class TestItNeverHoldsAWorkerThread:
    """
    check_and_wait() slept for up to two minutes and is called at the top of
    every request in app/github/client.py. Handlers run on a bounded pool, so
    six throttled calls stalled every webhook and the queue shed the rest.

    app/github/client.py fixed exactly this for the secondary rate limit and
    left a comment reading "Never sleep in a shared worker thread" — three
    lines below the call to this function, which did.
    """

    def test_a_long_reset_refuses_instead_of_sleeping(self):
        rl.update_from_headers(_headers(2, reset_at=time.time() + 1800), token="t")
        with patch("time.sleep") as slept:
            with pytest.raises(RuntimeError, match="rate limit exhausted"):
                rl.check_and_wait("t")
        slept.assert_not_called()

    def test_a_short_reset_is_ridden_out(self):
        """Refusing a request that would succeed three seconds later is worse
        than waiting three seconds. The line is drawn, not removed."""
        rl.update_from_headers(_headers(2, reset_at=time.time() + 3), token="t")
        with patch("time.sleep") as slept:
            rl.check_and_wait("t")
        slept.assert_called_once()
        assert slept.call_args[0][0] <= rl.DEFAULT_MAX_WAIT_SECONDS

    def test_the_ceiling_is_configurable(self, monkeypatch):
        monkeypatch.setenv("GITHUB_MAX_RATE_LIMIT_WAIT_SECONDS", "0")
        rl.update_from_headers(_headers(2, reset_at=time.time() + 3), token="t")
        with patch("time.sleep") as slept, pytest.raises(RuntimeError):
            rl.check_and_wait("t")
        slept.assert_not_called()

    def test_a_nonsense_ceiling_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("GITHUB_MAX_RATE_LIMIT_WAIT_SECONDS", "not-a-number")
        assert rl._max_wait_seconds() == float(rl.DEFAULT_MAX_WAIT_SECONDS)

    def test_a_healthy_budget_does_nothing_at_all(self):
        rl.update_from_headers(_headers(4000), token="t")
        with patch("time.sleep") as slept:
            rl.check_and_wait("t")
        slept.assert_not_called()

    def test_an_unknown_token_does_not_block(self):
        """First call from a new installation has no state. Refusing or
        sleeping there would break every cold start."""
        with patch("time.sleep") as slept:
            rl.check_and_wait("never-seen")
        slept.assert_not_called()


# ── The client must say whose limit it is ────────────────────────────────────


class TestTheClientReportsTheToken:
    def test_every_verb_passes_its_token_to_the_tracker(self):
        """Per-installation state is only correct if the call site supplies the
        token. A verb that forgets silently reopens the shared-counter bug."""
        import inspect

        from app.github import client

        src = inspect.getsource(client)
        assert src.count("check_and_wait(token)") == 5, "a verb is not passing its token"
        assert src.count('path, token)') >= 5, "a verb is not attributing its response headers"


# ── One installation's cold fetch must not block another ─────────────────────


class TestTokenFetchDoesNotSerialiseInstallations:
    """
    The token cache used a single global lock held across a 15-second network
    call, so a cache HIT for installation B waited behind a cold FETCH for
    installation A. With 8 gunicorn threads that serialises all GitHub work in
    the process behind one slow response.
    """

    @staticmethod
    def _response(token_value):
        r = MagicMock()
        r.json.return_value = {"token": token_value, "permissions": {"issues": "write"}}
        r.raise_for_status.return_value = None
        return r

    def test_a_cache_hit_does_not_wait_on_another_installations_fetch(self):
        from app.github import auth

        auth.clear_token_cache()
        started = threading.Event()
        release = threading.Event()

        def slow_post(url, **kw):
            started.set()
            release.wait(timeout=5)
            return self._response("tok-a")

        # Warm the cache for installation 2 so its read is a pure hit.
        with patch("app.github.auth.requests.post", return_value=self._response("tok-b")), patch(
            "app.github.auth.get_jwt", return_value="jwt"
        ):
            assert auth.get_installation_token(2) == "tok-b"

        result = {}

        def fetch_a():
            with patch("app.github.auth.requests.post", side_effect=slow_post), patch(
                "app.github.auth.get_jwt", return_value="jwt"
            ):
                result["a"] = auth.get_installation_token(1)

        t = threading.Thread(target=fetch_a, daemon=True)
        t.start()
        assert started.wait(timeout=5), "the slow fetch never began"

        # Installation 1's fetch is in flight. Installation 2 must answer now.
        began = time.time()
        assert auth.get_installation_token(2) == "tok-b"
        elapsed = time.time() - began

        release.set()
        t.join(timeout=5)

        assert elapsed < 1.0, (
            f"a cache hit for installation 2 took {elapsed:.2f}s while "
            "installation 1 was fetching — the lock is still global"
        )
        assert result.get("a") == "tok-a"
        auth.clear_token_cache()

    def test_concurrent_requests_for_one_installation_fetch_once(self):
        """The race the original lock existed to prevent must still be
        prevented: threads queueing on the same installation re-check the
        cache instead of each issuing its own token request."""
        from app.github import auth

        auth.clear_token_cache()
        calls = []

        def counting_post(url, **kw):
            calls.append(url)
            time.sleep(0.05)
            return self._response("tok")

        with patch("app.github.auth.requests.post", side_effect=counting_post), patch(
            "app.github.auth.get_jwt", return_value="jwt"
        ):
            threads = [
                threading.Thread(target=auth.get_installation_token, args=(99,))
                for _ in range(6)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        assert len(calls) == 1, f"{len(calls)} token requests for one installation"
        auth.clear_token_cache()

    def test_the_fetched_token_is_actually_cached(self):
        """A refactor that builds the cache entry and forgets to store it turns
        every call into a network round trip, silently."""
        from app.github import auth

        auth.clear_token_cache()
        with patch("app.github.auth.requests.post", return_value=self._response("tok")) as post, (
            patch("app.github.auth.get_jwt", return_value="jwt")
        ):
            auth.get_installation_token(7)
            auth.get_installation_token(7)
        assert post.call_count == 1, "the second call refetched — nothing was cached"
        assert auth.get_installation_permissions(7) == {"issues": "write"}
        auth.clear_token_cache()
