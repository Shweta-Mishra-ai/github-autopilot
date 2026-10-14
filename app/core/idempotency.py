"""
Idempotency - app/core/idempotency.py
Redis-backed event deduplication.

UNCHANGED from V5 except:
  - make_fingerprint uses full 32-char (128-bit) SHA256 truncation with
    clarified comment about actual collision properties.
  - In-memory fallback now logs a WARNING (not info) so operators notice
    when Redis is down and idempotency is weakened.

NOTE on fingerprint collision security:
  32 hex chars = 128 bits of output. Birthday bound is at 2^64 operations.
  With ~50k GitHub webhooks/day, collision probability is astronomically low.
  The sha256 prefix is collision-resistant for this use case.
"""

import hashlib
import logging
import threading
import time
from collections import OrderedDict

from app.core.redis_client import get_redis, is_redis_available

log = logging.getLogger(__name__)

_TTL_SECONDS = 86400  # 24h — GitHub retries for up to 24h
_MAX_LOCAL = 2000  # In-memory fallback max size

_seen_local: OrderedDict = OrderedDict()
# Webhooks are handled on several threads. Without this, two deliveries of
# one event could both find the fingerprint absent and both run, and one
# thread's pruning could delete the key another had just read (KeyError).
_local_lock = threading.Lock()

# Whether the last call fell back to memory. The fallback warning used to be
# emitted on EVERY event, and "Redis is unavailable" does not become more true
# the four-thousandth time it is logged — it becomes less readable. On a busy
# repository with Redis down, that single line buries every other warning in
# the log, and it accounted for a measurable share of the webhook path's own
# CPU time. Logged on the transition instead, in both directions, so an
# operator still learns when it starts AND when it recovers.
_in_fallback = False


def make_fingerprint(delivery_id: str, event_type: str, payload: dict) -> str:
    """
    A stable 32-char fingerprint for a webhook event: a hash of the payload
    itself.

    It hashed the delivery id, the event header and a few payload fields. The
    signature covers only the body — X-GitHub-Delivery and X-GitHub-Event are
    not signed — so anyone holding one captured signed body could replay it
    under a fresh delivery id (or another event header) and have it processed
    again. The payload is what GitHub signed, so it is what identifies the
    event. A genuine redelivery of an event that was not processed still runs:
    every path that does not accept an event releases its fingerprint.
    `delivery_id` and `event_type` are kept in the signature for callers.
    """
    import json

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def is_duplicate(fingerprint: str) -> bool:
    """
    Returns True if this event was already processed.
    Side effect: records fingerprint if new.

    Uses Redis SET NX — atomic, no TOCTOU race condition.
    Falls back to in-memory if Redis unavailable (with WARNING).
    """
    try:
        if is_redis_available():
            r = get_redis()
            key = f"idem:{fingerprint}"
            # Returns True (new key set) or None (key existed → duplicate)
            result = r.set(key, "1", nx=True, ex=_TTL_SECONDS)
            _leave_fallback()
            if result is None:
                log.info(f"idempotency.duplicate_redis fingerprint={fingerprint}")
                return True
            return False

    except Exception as e:
        _enter_fallback(f"redis_error error={e}")
    else:
        _enter_fallback("redis_unavailable")

    return _is_duplicate_local(fingerprint)


def forget(fingerprint: str) -> None:
    """
    Release a fingerprint recorded by is_duplicate(), for an event that was
    seen but NOT accepted (the server answered 503). Never raises.
    """
    with _local_lock:
        _seen_local.pop(fingerprint, None)
    try:
        if is_redis_available():
            get_redis().delete(f"idem:{fingerprint}")
    except Exception as e:
        log.warning(f"idempotency.forget_failed fingerprint={fingerprint}: {e}")


def _enter_fallback(reason: str) -> None:
    """Log the transition into memory-only dedup, once per episode."""
    global _in_fallback
    if not _in_fallback:
        _in_fallback = True
        log.warning(
            f"idempotency.using_memory_fallback ({reason}) — duplicate events "
            "are possible across restarts until Redis returns. This is logged "
            "once, not per event; recovery is logged too."
        )


def _leave_fallback() -> None:
    global _in_fallback
    if _in_fallback:
        _in_fallback = False
        log.warning("idempotency.redis_recovered — dedup is durable again")


def _is_duplicate_local(fingerprint: str) -> bool:
    """In-memory fallback. Not safe across restarts; safe across threads."""
    with _local_lock:
        return _check_and_record_local(fingerprint)


def _check_and_record_local(fingerprint: str) -> bool:
    now = time.time()

    # Pop from the front while the oldest entry is expired, rather than
    # scanning every entry. _seen_local is an OrderedDict written in time
    # order, so the oldest key is always first and everything after the first
    # live entry is live too — the full scan was O(n) per event over up to
    # _MAX_LOCAL entries, on the hot path, to find the handful that aged out.
    cutoff = now - _TTL_SECONDS
    while _seen_local:
        oldest_key = next(iter(_seen_local))
        if _seen_local[oldest_key] > cutoff:
            break
        del _seen_local[oldest_key]

    while len(_seen_local) > _MAX_LOCAL:
        _seen_local.popitem(last=False)

    if fingerprint in _seen_local:
        log.info(f"idempotency.duplicate_local fingerprint={fingerprint}")
        return True

    _seen_local[fingerprint] = now
    return False
