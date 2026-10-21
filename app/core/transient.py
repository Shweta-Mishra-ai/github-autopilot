"""
Which failures are worth retrying, and a guard for side effects a retry must
not repeat.

The queue retries an event only when the handler raises a transient error.
Handlers used to catch those errors themselves and return normally, so the
outcome was OK and the event was gone: GitHub had already been answered 202
and never redelivers. A handler now raises a transient failure, and wraps
any side effect that a second run must not repeat in once().
"""

import contextlib
import logging
import time

log = logging.getLogger(__name__)


class Busy(Exception):
    """Another worker holds what this one needs; the event can run later."""


@contextlib.contextmanager
def held(key: str, ttl_seconds: int = 60, wait_seconds: float = 20.0):
    """
    Hold a Redis lock for a read-modify-write that two workers must not
    interleave. Raises Busy (transient: the queue retries the event) when it
    cannot be had within `wait_seconds`, and when Redis cannot be reached —
    without the lock, the write is the race it exists to prevent.

    Released only by its holder: a holder that outlived the TTL must not
    delete the next holder's lock.
    """
    import secrets

    from app.core.redis_client import get_redis

    name = f"lock:{key}"
    token = secrets.token_hex(8)
    deadline = time.monotonic() + wait_seconds
    try:
        r = get_redis()
        while not r.set(name, token, nx=True, ex=ttl_seconds):
            if time.monotonic() >= deadline:
                raise Busy(f"{key} is held by another worker")
            time.sleep(0.25)
    except Busy:
        raise
    except Exception as e:
        raise Busy(f"lock {key} unavailable: {type(e).__name__}") from e
    try:
        yield
    finally:
        try:
            if r.get(name) in (token, token.encode()):
                r.delete(name)
        except Exception as e:  # nosec B110 - the TTL frees it
            log.warning(f"transient.unlock_failed key={key}: {type(e).__name__}")


def is_transient(e: BaseException) -> bool:
    """A failure that the same event can succeed after, a little later."""
    import requests

    from app.ai.circuit_breaker import AllProvidersDown
    from app.core.config import ConfigUnavailable
    from app.github.client import GitHubError
    from app.github.rate_limit import GitHubRateLimitExhausted

    if isinstance(
        e,
        (
            Busy,
            GitHubRateLimitExhausted,
            AllProvidersDown,
            ConfigUnavailable,
            requests.ConnectionError,
            requests.Timeout,
        ),
    ):
        return True
    if isinstance(e, GitHubError):
        # Secondary rate limits are 403s with their own type. Status 0 is the
        # client's code for a connection error or timeout.
        return (
            e.status_code in (0, 429)
            or e.status_code >= 500
            or type(e).__name__ == "GitHubSecondaryRateLimitError"
        )
    return False


def once(key: str, ttl_seconds: int = 86400) -> bool:
    """
    True for the first caller with this key within the TTL, False after.

    For a side effect that a retried event must not repeat — a Slack alert
    sent before the step that failed. With Redis unreachable it answers True:
    a duplicate notification is a smaller failure than a missing one.
    """
    try:
        from app.core.redis_client import get_redis

        return get_redis().set(f"once:{key}", "1", nx=True, ex=ttl_seconds) is not None
    except Exception as e:
        log.warning(f"once.redis_unavailable key={key}: {e} — proceeding")
        return True
