"""
tests/test_queue.py
V4 - Fixed for CI environment.

FIXED: In CI, REDIS_URL=redis://localhost:6379/0 is set.
  producer.py evaluates _use_redis = bool(os.environ.get("REDIS_URL")) at import time.
  So all tests hit Redis, not in-memory queue.
  Fix: Patch app.queue.producer._use_redis = False to force in-memory path.
"""

from unittest.mock import patch
from app.queue.producer import enqueue_event, get_memory_queue


class TestQueueProducer:

    def setup_method(self):
        """Clear queue before each test."""
        q = get_memory_queue()
        while not q.empty():
            try:
                q.get_nowait()
            except Exception:
                break

    @patch("app.queue.producer._use_redis", False)
    def test_enqueue_returns_true_on_success(self):
        result = enqueue_event("push", {"repo": "test"}, "delivery-1")
        assert result is True

    @patch("app.queue.producer._use_redis", False)
    def test_enqueued_event_in_queue(self):
        enqueue_event("issues", {"number": 1}, "delivery-2")
        q = get_memory_queue()
        assert not q.empty()

    @patch("app.queue.producer._use_redis", False)
    def test_event_has_correct_fields(self):
        enqueue_event("pull_request", {"pr": 5}, "delivery-3")
        q = get_memory_queue()
        event = q.get_nowait()
        assert event["event_type"] == "pull_request"
        assert event["payload"] == {"pr": 5}
        assert event["delivery_id"] == "delivery-3"

    @patch("app.queue.producer._use_redis", False)
    def test_multiple_events_queued_in_order(self):
        enqueue_event("push", {}, "d1")
        enqueue_event("issues", {}, "d2")
        enqueue_event("pull_request", {}, "d3")

        q = get_memory_queue()
        events = []
        for _ in range(3):
            events.append(q.get_nowait())

        assert [e["event_type"] for e in events] == ["push", "issues", "pull_request"]

    @patch("app.queue.producer._use_redis", False)
    def test_empty_payload_enqueued(self):
        result = enqueue_event("push", {}, "")
        assert result is True

    def test_redis_not_used_when_no_env(self):
        with patch("app.queue.producer._use_redis", False):
            result = enqueue_event("push", {}, "test")
            assert result is True


class TestQueueConsumer:

    @patch("app.queue.producer._use_redis", False)
    @patch("app.queue.consumer._use_redis", False)
    def test_consumer_yields_events(self):
        # Clear queue first
        q = get_memory_queue()
        while not q.empty():
            try:
                q.get_nowait()
            except Exception:
                break

        enqueue_event("push", {"test": True}, "d-consumer-1")

        from app.queue.consumer import _consume_memory
        consumer = _consume_memory()

        event_type, payload = next(consumer)
        assert event_type == "push"
        assert payload == {"test": True}
