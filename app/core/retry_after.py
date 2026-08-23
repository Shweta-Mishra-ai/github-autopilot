"""
app/core/retry_after.py — tolerant Retry-After header parsing.

WHY THIS EXISTS
    Three call sites parsed this header with a bare ``int(header)``:
    the GitHub client's 429 and secondary-rate-limit branches, and the
    Groq provider's 429 branch. Every one of them breaks on a header
    that RFC 9110 explicitly permits, and on one a real provider sends:

      * ``Retry-After: Wed, 21 Oct 2015 07:28:00 GMT`` — the HTTP-date
        form. ``int()`` raises ValueError.
      * ``Retry-After: 7.66`` — fractional seconds, which Groq returns.
        ``int()`` raises ValueError on the string form.

    The consequences differed per site but were never harmless: an
    uncaught ValueError escaping the GitHub client (callers only catch
    GitHubError), a transient rate limit downgraded to a permanent-looking
    403 Forbidden, and a circuit breaker tripped with the wrong reason.

    Parsing is deliberately total: it never raises, and anything it
    cannot make sense of falls back to the caller's default.
"""

import math
import time
from email.utils import parsedate_to_datetime

# A server that asks us to wait longer than this is either confused or
# hostile; either way we clamp rather than propagate an absurd delay.
MAX_RETRY_AFTER = 3600


def parse_retry_after(value, default: int = 30) -> int:
    """
    Return a whole number of seconds to wait, from a Retry-After header.

    Accepts delay-seconds (integer or fractional) and the HTTP-date form.
    Fractional delays round up, so the wait is never shorter than asked.
    A date already in the past yields 0. Anything unparseable — including
    None, empty strings and negatives — yields ``default``.
    """
    if value is None:
        return default

    text = str(value).strip()
    if not text:
        return default

    try:
        seconds = float(text)
    except ValueError:
        pass
    else:
        if not math.isfinite(seconds) or seconds < 0:
            return default
        return min(math.ceil(seconds), MAX_RETRY_AFTER)

    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return default
    if when is None:
        return default

    try:
        delta = when.timestamp() - time.time()
    except (OverflowError, OSError, ValueError):
        return default

    if delta <= 0:
        return 0
    return min(math.ceil(delta), MAX_RETRY_AFTER)
