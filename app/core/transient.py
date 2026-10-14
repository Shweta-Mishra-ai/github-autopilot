"""
Which failures are worth retrying, and a guard for side effects a retry must
not repeat.

The queue retries an event only when the handler raises a transient error.
Handlers used to catch those errors themselves and return normally, so the
outcome was OK and the event was gone: GitHub had already been answered 202
and never redelivers. A handler now raises a transient failure, and wraps
any side effect that a second run must not repeat in once().
"""

import logging

log = logging.getLogger(__name__)


def is_transient(e: BaseException) -> bool:
    """A failure that the same event can succeed after, a little later."""
    import requests

    from app.ai.circuit_breaker import AllProvidersDown
    from app.github.client import GitHubError
    from app.github.rate_limit import GitHubRateLimitExhausted

    if isinstance(
        e, (GitHubRateLimitExhausted, AllProvidersDown, requests.ConnectionError, requests.Timeout)
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
