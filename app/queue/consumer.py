"""
Queue Consumer - app/queue/consumer.py
Yields events from queue for worker processing.
V3: Redis Streams with in-memory fallback.
"""

import json
import os
import time
from typing import Generator, Tuple
from app.core.logger import get_logger

log = get_logger(__name__)

_use_redis = bool(os.environ.get("REDIS_URL"))


def consume_events() -> Generator[Tuple[str, dict], None, None]:
    """Yields (event_type, payload) tuples indefinitely."""
    if _use_redis:
        yield from _consume_redis()
    else:
        yield from _consume_memory()


def _consume_memory() -> Generator[Tuple[str, dict], None, None]:
    from app.queue.producer import get_memory_queue
    q = get_memory_queue()
    log.info("consumer.started.memory")
    while True:
        try:
            event = q.get(timeout=1.0)
            yield event["event_type"], event["payload"]
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

    # Create consumer group if not exists
    try:
        r.xgroup_create(stream, consumer_group, id="0", mkstream=True)
    except Exception:
        pass  # Group already exists

    log.info("consumer.started.redis", stream=stream)

    while True:
        try:
            results = r.xreadgroup(
                consumer_group, consumer_name,
                {stream: ">"}, count=1, block=1000
            )
            if not results:
                continue
            for _, messages in results:
                for msg_id, data in messages:
                    event = json.loads(data[b"data"])
                    yield event["event_type"], event["payload"]
                    r.xack(stream, consumer_group, msg_id)
        except Exception as e:
            log.error("consumer.error.redis", error=str(e))
            time.sleep(2)

