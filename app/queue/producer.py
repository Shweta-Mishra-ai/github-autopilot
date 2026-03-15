"""
Queue Producer - app/queue/producer.py
Enqueues webhook events for async processing.
V3: Uses Redis Streams if available, falls back to in-memory queue.
"""

import json
import os
import queue
import threading
from app.core.logger import get_logger

log = get_logger(__name__)

# In-memory fallback queue (used when Redis is not configured)
_memory_queue: queue.Queue = queue.Queue()
_use_redis = bool(os.environ.get("REDIS_URL"))


def enqueue_event(event_type: str, payload: dict, delivery_id: str = "") -> bool:
    event = {
        "event_type": event_type,
        "payload": payload,
        "delivery_id": delivery_id,
    }

    if _use_redis:
        return _enqueue_redis(event)
    else:
        return _enqueue_memory(event)


def _enqueue_memory(event: dict) -> bool:
    try:
        _memory_queue.put_nowait(event)
        log.debug("queue.enqueued.memory", event=event["event_type"])
        return True
    except queue.Full:
        log.error("queue.full.memory")
        return False


def _enqueue_redis(event: dict) -> bool:
    try:
        import redis
        r = redis.from_url(os.environ["REDIS_URL"])
        r.xadd("ai_repo_manager:events", {"data": json.dumps(event)})
        log.debug("queue.enqueued.redis", event=event["event_type"])
        return True
    except Exception as e:
        log.error("queue.enqueue_failed.redis", error=str(e))
        # Fallback to memory queue
        return _enqueue_memory(event)


def get_memory_queue() -> queue.Queue:
    """Expose memory queue for consumer."""
    return _memory_queue
