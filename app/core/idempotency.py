"""
Idempotency - app/core/idempotency.py
V4: Redis-backed event deduplication.

FIXED (LOOPHOLE 9):
  Old: In-memory OrderedDict — all fingerprints lost on every app restart.
  Problem: GitHub retries webhooks for up to 24 hours.
           After a Render deploy restart, the app re-processed old events →
           double comments, double labels, double AI calls.
  Fix: Redis SET NX (atomic check-and-set) survives restarts.
       Falls back to in-memory if Redis is unavailable (dev mode).

NX flag = "only set if Not eXists" → atomic, no race condition.
"""

import hashlib
import time
import logging
from collections import OrderedDict

log = logging.getLogger(__name__)

_TTL_SECONDS = 3600  # Remember events for 1 hour
_MAX_LOCAL = 2000  # In-memory fallback max size

# In-memory fallback (used when Redis is unavailable)
_seen_local: OrderedDict = OrderedDict()


def make_fingerprint(delivery_id: str, event_type: str, payload: dict) -> str:
    """
    Create a stable, short fingerprint for a webhook event.
    Uses delivery_id (unique per GitHub delivery) + key payload fields.
    """
    key_fields = {
        "delivery": delivery_id,
        "event": event_type,
        "action": payload.get("action", ""),
        "repo": payload.get("repository", {}).get("full_name", ""),
        "number": (
            payload.get("pull_request", {}).get("number")
            or payload.get("issue", {}).get("number")
            or payload.get("comment", {}).get("id")
            or ""
        ),
    }
    raw = "|".join(str(v) for v in key_fields.values())
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def is_duplicate(fingerprint: str) -> bool:
    """
    Returns True if this event was already processed.
    Side effect: records fingerprint if new (so next call returns True).

    Uses Redis SET NX — atomic, no TOCTOU race condition.
    Falls back to in-memory if Redis unavailable.
    """
    # ── Try Redis first ──────────────────────────────────────────────────────
    try:
        from app.core.redis_client import get_redis, is_redis_available

        if is_redis_available():
            r = get_redis()
            key = f"idem:{fingerprint}"

            # SET key "1" NX EX 3600
            # Returns True  if key was NEW (set successfully) → not duplicate
            # Returns None  if key EXISTED → duplicate
            result = r.set(key, "1", nx=True, ex=_TTL_SECONDS)

            if result is None:
                log.info(f"idempotency.duplicate_redis fingerprint={fingerprint}")
                return True  # Already processed

            return False  # New event, just recorded

    except Exception as e:
        log.warning(f"idempotency.redis_error fallback_to_memory error={e}")

    # ── In-memory fallback ───────────────────────────────────────────────────
    return _is_duplicate_local(fingerprint)


def _is_duplicate_local(fingerprint: str) -> bool:
    """In-memory fallback. Not safe across restarts — use only when Redis down."""
    now = time.time()

    # Evict expired entries
    expired = [k for k, ts in _seen_local.items() if now - ts > _TTL_SECONDS]
    for k in expired:
        del _seen_local[k]

    # Evict oldest if over max size
    while len(_seen_local) > _MAX_LOCAL:
        _seen_local.popitem(last=False)

    if fingerprint in _seen_local:
        log.info(f"idempotency.duplicate_local fingerprint={fingerprint}")
        return True

    _seen_local[fingerprint] = now
    return False
