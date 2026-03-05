"""
Idempotency - app/core/idempotency.py
Prevents processing same webhook event twice.
Uses event fingerprint = sha256(delivery_id + event_type + payload_hash)
"""

import hashlib
import time
import logging
from collections import OrderedDict

log = logging.getLogger(__name__)

# TTL-based cache: {fingerprint: timestamp}
_seen: OrderedDict = OrderedDict()
_TTL_SECONDS = 3600   # forget events after 1 hour
_MAX_SIZE = 2000       # max entries before eviction


def _evict_expired():
    """Remove entries older than TTL."""
    now = time.time()
    expired = [k for k, t in _seen.items() if now - t > _TTL_SECONDS]
    for k in expired:
        del _seen[k]

    # Also evict oldest if over max size
    while len(_seen) > _MAX_SIZE:
        _seen.popitem(last=False)


def make_fingerprint(delivery_id: str, event_type: str, payload: dict) -> str:
    """
    Create a stable fingerprint for a webhook event.
    delivery_id is the X-GitHub-Delivery header (unique per GitHub delivery).
    We include it so retried deliveries with same payload are still deduplicated.
    """
    # Key fields that identify a unique action
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
    Side effect: records fingerprint if new.
    """
    _evict_expired()

    if fingerprint in _seen:
        log.info(f"Duplicate event detected: {fingerprint}")
        return True

    _seen[fingerprint] = time.time()
    return False
