"""
Queue Producer - app/queue/producer.py
V3: Enqueues webhook events for async processing.
Uses Redis Streams if available, falls back to in-memory queue.
NOTE: Never use event= in log calls (structlog reserved keyword).
"""

import json
import os
import queue
from app.core.logger import get_logger

log = get_logger(__name__)

_memory_queue: queue.Queue = queue.Queue()
_use_redis = bool(os.environ.get("REDIS_URL"))


def enqueue_event(webhook_event: str, payload: dict, delivery_id: str = "") -> bool:
    item = {
        "event_type": webhook_event,
        "payload": payload,
        "delivery_id": delivery_id,
    }

    if _use_redis:
        return _enqueue_redis(item)
    else:
        return _enqueue_memory(item)


def _enqueue_memory(item: dict) -> bool:
    try:
        _memory_queue.put_nowait(item)
        # NOTE: Use webhook_event= NOT event=
        log.debug("queue_enqueued_memory", webhook_event=item["event_type"])
        return True
    except queue.Full:
        log.error("queue_full_memory")
        return False


def _enqueue_redis(item: dict) -> bool:
    try:
        import redis

        r = redis.from_url(os.environ["REDIS_URL"])
        r.xadd("ai_repo_manager:events", {"data": json.dumps(item)})
        log.debug("queue_enqueued_redis", webhook_event=item["event_type"])
        return True
    except Exception as e:
        log.error("queue_enqueue_failed", error=str(e))
        return _enqueue_memory(item)


def get_memory_queue() -> queue.Queue:
    return _memory_queue
