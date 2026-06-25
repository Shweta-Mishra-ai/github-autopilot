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
import time
import logging
from collections import OrderedDict

from app.core.redis_client import get_redis, is_redis_available

log = logging.getLogger(__name__)

_TTL_SECONDS = 86400  # 24h — GitHub retries for up to 24h
_MAX_LOCAL   = 2000   # In-memory fallback max size

_seen_local: OrderedDict = OrderedDict()


def make_fingerprint(delivery_id: str, event_type: str, payload: dict) -> str:
    """
    Create a stable 32-char fingerprint for a webhook event.
    Uses delivery_id (unique per GitHub delivery) + key payload fields.
    """
    key_fields = {
        "delivery": delivery_id,
        "event":    event_type,
        "action":   payload.get("action", ""),
        "repo":     payload.get("repository", {}).get("full_name", ""),
        "number": (
            payload.get("pull_request", {}).get("number")
            or payload.get("issue", {}).get("number")
            or payload.get("comment", {}).get("id")
            or ""
        ),
    }
    raw = "|".join(str(v) for v in key_fields.values())
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def is_duplicate(fingerprint: str) -> bool:
    """
    Returns True if this event was already processed.
    Side effect: records fingerprint if new.

    Uses Redis SET NX — atomic, no TOCTOU race condition.
    Falls back to in-memory if Redis unavailable (with WARNING).
    """
    try:
        if is_redis_available():
            r   = get_redis()
            key = f"idem:{fingerprint}"
            # Returns True (new key set) or None (key existed → duplicate)
            result = r.set(key, "1", nx=True, ex=_TTL_SECONDS)
            if result is None:
                log.info(f"idempotency.duplicate_redis fingerprint={fingerprint}")
                return True
            return False

    except Exception as e:
        log.warning(f"idempotency.redis_error fallback_to_memory error={e}")

    # In-memory fallback — weaker (lost on restart, process-local)
    log.warning(
        "idempotency.using_memory_fallback — Redis unavailable. "
        "Duplicate events possible across restarts."
    )
    return _is_duplicate_local(fingerprint)


def _is_duplicate_local(fingerprint: str) -> bool:
    """In-memory fallback. Not safe across restarts."""
    now = time.time()

    expired = [k for k, ts in _seen_local.items() if now - ts > _TTL_SECONDS]
    for k in expired:
        del _seen_local[k]

    while len(_seen_local) > _MAX_LOCAL:
        _seen_local.popitem(last=False)

    if fingerprint in _seen_local:
        log.info(f"idempotency.duplicate_local fingerprint={fingerprint}")
        return True

    _seen_local[fingerprint] = now
    return False
