"""
Queue Consumer - app/queue/consumer.py
V3: Yields events from queue for worker processing.
Uses Redis Streams if available, falls back to in-memory queue.
NOTE: Never use event= in log calls (structlog reserved keyword).
"""

import json
import os
import time
from typing import Generator, Tuple
from app.core.logger import get_logger

log = get_logger(__name__)

_use_redis = bool(os.environ.get("REDIS_URL"))


def consume_events() -> Generator[Tuple[str, dict], None, None]:
    """Yields (webhook_event, payload) tuples indefinitely."""
    if _use_redis:
        yield from _consume_redis()
    else:
        yield from _consume_memory()


def _consume_memory() -> Generator[Tuple[str, dict], None, None]:
    from app.queue.producer import get_memory_queue

    q = get_memory_queue()
    log.info("consumer_started_memory")
    while True:
        try:
            item = q.get(timeout=1.0)
            yield item["event_type"], item["payload"]
            q.task_done()
        except Exception:
            time.sleep(0.1)
            continue


def _consume_redis() -> Generator[Tuple[str, dict], None, None]:
    import redis

    r = redis.from_url(os.environ["REDIS_URL"])
    stream = "ai_repo_manager:events"
    consumer_group = "workers"
    consumer_name = f"worker-{os.getpid()}"

    try:
        r.xgroup_create(stream, consumer_group, id="0", mkstream=True)
    except Exception:
        pass

    log.info("consumer_started_redis", stream=stream)

    while True:
        try:
            results = r.xreadgroup(
                consumer_group, consumer_name, {stream: ">"}, count=1, block=1000
            )
            if not results:
                continue
            for _, messages in results:
                for msg_id, data in messages:
                    item = json.loads(data[b"data"])
                    yield item["event_type"], item["payload"]
                    r.xack(stream, consumer_group, msg_id)
        except Exception as e:
            log.error("consumer_redis_error", error=str(e))
            time.sleep(2)
