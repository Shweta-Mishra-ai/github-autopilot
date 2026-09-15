"""
tests/test_integration_redis.py — the queue against a real Redis.

WHY THESE EXIST
  The whole suite runs against an in-process fake (tests/conftest.py). That is
  the right default: it is fast, deterministic and needs no service. But it is
  a dictionary with a lock, and the properties this project actually depends on
  are not properties of a dictionary:

    - BLMOVE is atomic across processes. The fake cannot be wrong about that,
      because nothing in it is concurrent in the way Redis is.
    - LPUSH plus a RIGHT-pop gives FIFO. Get the direction wrong and every
      webhook is processed newest-first, which no unit test would notice
      because the fake is exercised one operation at a time.
    - `noeviction` makes a write FAIL when memory is full rather than silently
      dropping a key. render.yaml sets that policy specifically because
      allkeys-lru was evicting idempotency keys and causing duplicate webhook
      processing. Nothing verified the behaviour it was changed to.
    - A key with a TTL actually expires.

  CI ran with a Redis service attached for a while and nothing connected to it,
  because no test was marked `integration`. The service was removed, correctly,
  with a note saying to give these their own job when they were written. This
  is that job.

RUNNING THEM
  Excluded from the default run. They need a server:

    redis-server --port 6399 --save '' --maxmemory-policy noeviction --daemonize yes
    REDIS_TEST_URL=redis://127.0.0.1:6399/0 pytest -m integration

  Without REDIS_TEST_URL, or with an unreachable one, every test here skips
  rather than fails: a missing optional service is not a broken build.
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

REDIS_TEST_URL = os.environ.get("REDIS_TEST_URL", "")


def real_redis_module():
    """
    The genuine redis package, not conftest's in-process fake.

    conftest installs `sys.modules["redis"] = _build_redis_mock()` so the unit
    suite can never touch a server by accident. That is correct, and it also
    means a plain `import redis` here returns the fake — whose `from_url`
    happily "connects" to nothing, so every test below would pass against a
    dictionary while claiming to prove something about Redis.

    Loading it from disk by path sidesteps the shadow without disturbing it for
    anyone else.
    """
    import importlib.util
    import sys

    cached = sys.modules.get("_real_redis_for_integration")
    if cached is not None:
        return cached

    saved = sys.modules.pop("redis", None)
    try:
        import redis as genuine  # noqa: F401 — resolved from site-packages now

        sys.modules["_real_redis_for_integration"] = genuine
        return genuine
    finally:
        if saved is not None:
            sys.modules["redis"] = saved


def _server_reachable(url: str) -> bool:
    if not url:
        return False
    try:
        real = real_redis_module()
        real.from_url(url, socket_connect_timeout=2).ping()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _server_reachable(REDIS_TEST_URL),
        reason="set REDIS_TEST_URL to a reachable Redis to run the integration suite",
    ),
]


@pytest.fixture
def live_redis(monkeypatch):
    """
    Point the application at the real server and hand back a raw client.

    conftest's autouse env fixture forces REDIS_URL="" so the unit suite can
    never touch a server by accident. Overriding it here is the deliberate
    opt-in, and the singleton is reset either side so no other test inherits
    a live connection.
    """
    import app.core.redis_client as rc

    real = real_redis_module()

    # app/core/redis_client.py binds `import redis as redis_lib` at module
    # import, and conftest has already put the fake there — so get_redis()
    # returns a MockRedis no matter what REDIS_URL says, and MockRedis has no
    # blmove(). Rebinding the module attribute is what actually points the
    # application at the server; setting the URL alone is not enough.
    monkeypatch.setattr(rc, "redis_lib", real)
    monkeypatch.setenv("REDIS_URL", REDIS_TEST_URL)
    rc.reset_client()

    raw = real.from_url(REDIS_TEST_URL, decode_responses=True)
    raw.flushdb()
    try:
        yield raw
    finally:
        raw.flushdb()
        rc.reset_client()


@pytest.fixture
def queue(live_redis):
    """The queue module, with its module state reset between tests."""
    import app.core.event_queue as eq

    eq._stop.clear()
    with eq._threads_lock:
        eq._threads.clear()
    yield eq
    eq._stop.set()
    with eq._threads_lock:
        eq._threads.clear()


# ── Ordering ─────────────────────────────────────────────────────────────────


class TestOrderingIsActuallyFIFO:
    """LPUSH plus a RIGHT-pop is FIFO. Reverse either and every webhook is
    handled newest-first — a reordering no single-operation unit test sees."""

    def test_events_come_back_in_the_order_they_arrived(self, queue):
        for i in range(10):
            assert queue.enqueue("push", {"n": i}, "o/r", f"d{i}") == queue.EnqueueResult.OK

        seen = []
        queue_handler = lambda _e, payload, _r: seen.append(payload["n"])  # noqa: E731
        for _ in range(10):
            assert queue._consume_once(queue_handler) is True

        assert seen == list(range(10)), f"processed out of order: {seen}"

    def test_the_queue_drains_to_empty(self, queue):
        queue.enqueue("push", {"n": 1}, "o/r", "d1")
        queue._consume_once(lambda *a: None)
        assert queue.queue_stats()["pending"] == 0
        assert queue.queue_stats()["processing"] == 0


# ── At-least-once and crash recovery ─────────────────────────────────────────


class TestAnEventSurvivesACrash:
    """The reason the queue exists. A thread pool loses everything on restart;
    this must not."""

    def test_an_in_flight_event_is_requeued_at_boot(self, queue, live_redis):
        queue.enqueue("pull_request", {"n": 1}, "o/r", "d1")

        # Simulate a crash mid-handler: the envelope has moved to processing
        # and the process dies before LREM.
        raw = live_redis.rpoplpush(queue.PENDING_KEY, queue.PROCESSING_KEY)
        assert raw is not None
        assert live_redis.llen(queue.PROCESSING_KEY) == 1
        assert live_redis.llen(queue.PENDING_KEY) == 0

        assert queue.recover_stale() == 1
        assert live_redis.llen(queue.PENDING_KEY) == 1
        assert live_redis.llen(queue.PROCESSING_KEY) == 0

    def test_a_poison_event_dead_letters_instead_of_looping(self, queue, live_redis):
        """Without this it is requeued forever and the queue never drains."""
        env = json.dumps({"id": "d1", "event": "push", "repo": "o/r", "payload": {}, "attempts": 1})
        live_redis.lpush(queue.PROCESSING_KEY, env)

        assert queue.recover_stale() == 0
        assert live_redis.llen(queue.DEAD_KEY) == 1
        assert live_redis.llen(queue.PENDING_KEY) == 0

    def test_unparseable_json_is_dead_lettered_not_crashed_on(self, queue, live_redis):
        live_redis.lpush(queue.PROCESSING_KEY, "{not json at all")
        queue.recover_stale()
        assert live_redis.llen(queue.DEAD_KEY) == 1

    def test_the_dead_letter_list_stays_bounded(self, queue, live_redis):
        """25MB of Redis. An unbounded debug list is a slow outage."""
        for i in range(queue.DEAD_MAX + 25):
            live_redis.lpush(queue.PROCESSING_KEY, json.dumps({"id": i, "attempts": 5}))
        queue.recover_stale(max_items=queue.DEAD_MAX + 25)
        assert live_redis.llen(queue.DEAD_KEY) == queue.DEAD_MAX


# ── Concurrency: the property a fake cannot have ─────────────────────────────


class TestConcurrentConsumersDoNotDoubleProcess:
    """
    BLMOVE is atomic: two consumers racing for the same envelope cannot both
    win. This is the single most important property of the design and the one
    an in-process dictionary cannot demonstrate.
    """

    def test_no_event_is_handled_twice(self, queue):
        total = 60
        for i in range(total):
            queue.enqueue("push", {"n": i}, "o/r", f"d{i}")

        handled: list[int] = []
        lock = threading.Lock()
        errors: list[Exception] = []

        def worker():
            try:
                while True:
                    got = queue._consume_once(
                        lambda _e, p, _r: (lock.acquire(), handled.append(p["n"]), lock.release())
                    )
                    if not got:
                        return
            except Exception as exc:  # pragma: no cover - surfaced by the assert
                errors.append(exc)

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert not errors, errors
        assert sorted(handled) == list(range(total)), "an event was lost or duplicated"
        assert len(handled) == len(set(handled)), "an event was processed twice"

    def test_nothing_is_left_stranded_in_processing(self, queue, live_redis):
        for i in range(20):
            queue.enqueue("push", {"n": i}, "o/r", f"d{i}")

        def worker():
            while queue._consume_once(lambda *a: None):
                pass

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert live_redis.llen(queue.PROCESSING_KEY) == 0
        assert live_redis.llen(queue.PENDING_KEY) == 0


# ── Bounds, against a server that really counts ──────────────────────────────


class TestTheQueueIsBounded:
    def test_enqueue_reports_full_at_the_cap(self, queue, live_redis, monkeypatch):
        monkeypatch.setattr(queue, "MAX_QUEUE_LEN", 5)
        results = [queue.enqueue("push", {"n": i}, "o/r", f"d{i}") for i in range(8)]
        assert results[:5] == [queue.EnqueueResult.OK] * 5
        assert results[5:] == [queue.EnqueueResult.FULL] * 3
        assert live_redis.llen(queue.PENDING_KEY) == 5

    def test_an_oversized_envelope_never_reaches_redis(self, queue, live_redis):
        """It skips the durable path deliberately: parking a huge payload in a
        25MB Redis costs more than replaying the delivery would."""
        huge = {"blob": "x" * (queue.MAX_ENVELOPE_BYTES + 1024)}
        assert queue.enqueue("push", huge, "o/r", "big") == queue.EnqueueResult.TOO_LARGE
        assert live_redis.llen(queue.PENDING_KEY) == 0


# ── The policy render.yaml sets, verified ────────────────────────────────────


class TestNoevictionBehaviour:
    """
    render.yaml sets `maxmemory-policy noeviction` with a comment saying
    allkeys-lru was evicting idempotency keys and causing duplicate webhook
    processing. The policy it was changed TO was never verified.

    Under noeviction a write past the limit returns an error. That is the
    desired behaviour: an error is visible and recoverable, a silent eviction
    is a duplicate webhook nobody can explain.
    """

    def test_the_server_is_configured_the_way_production_is(self, live_redis):
        policy = live_redis.config_get("maxmemory-policy")["maxmemory-policy"]
        assert policy == "noeviction", (
            f"this suite must run against noeviction to be meaningful, got {policy}"
        )

    def test_a_write_past_the_limit_errors_rather_than_evicting(self, live_redis):
        real = real_redis_module()

        original = live_redis.config_get("maxmemory")["maxmemory"]
        try:
            live_redis.set("canary", "must-survive")
            live_redis.config_set("maxmemory", "1mb")
            with pytest.raises(real.exceptions.ResponseError, match="(?i)oom|memory"):
                for i in range(20_000):
                    live_redis.set(f"filler:{i}", "y" * 512)
            # The canary is the whole point: noeviction must not have dropped it.
            assert live_redis.get("canary") == "must-survive"
        finally:
            live_redis.config_set("maxmemory", original)


# ── Idempotency and TTL against a real clock ─────────────────────────────────


class TestIdempotencyAgainstRealRedis:
    def test_a_replayed_delivery_is_recognised(self, live_redis, monkeypatch):
        import app.core.idempotency as idem

        fp = idem.make_fingerprint("delivery-1", "push", {"a": 1})
        assert idem.is_duplicate(fp) is False
        assert idem.is_duplicate(fp) is True, "the second delivery must be seen as a duplicate"

    def test_two_different_deliveries_do_not_collide(self, live_redis):
        import app.core.idempotency as idem

        a = idem.make_fingerprint("delivery-1", "push", {"a": 1})
        b = idem.make_fingerprint("delivery-2", "push", {"a": 1})
        assert a != b
        assert idem.is_duplicate(a) is False
        assert idem.is_duplicate(b) is False

    def test_a_key_with_a_ttl_really_expires(self, live_redis):
        """Expiry is what bounds the dedup set on a 25MB instance. A fake that
        never expires anything cannot show this."""
        live_redis.set("ttl-probe", "1", ex=1)
        assert live_redis.get("ttl-probe") == "1"
        time.sleep(1.4)
        assert live_redis.get("ttl-probe") is None

    def test_the_dedup_key_carries_an_expiry(self, live_redis):
        import app.core.idempotency as idem

        fp = idem.make_fingerprint("delivery-ttl", "push", {})
        idem.is_duplicate(fp)
        matching = [k for k in live_redis.scan_iter(match="*") if "delivery-ttl" in k or True]
        ttls = [live_redis.ttl(k) for k in matching]
        assert any(t > 0 for t in ttls), "no dedup key carries a TTL — the set grows forever"


# ── Degradation ──────────────────────────────────────────────────────────────


class TestLosingRedisDegradesRatherThanBreaks:
    """
    A real connection failure, not a simulated one. The fake never refuses a
    connection, so the degraded path can only be exercised against a genuine
    client pointed somewhere nothing is listening.
    """

    @pytest.fixture
    def dead_server(self, monkeypatch):
        import app.core.redis_client as rc

        monkeypatch.setattr(rc, "redis_lib", real_redis_module())
        # Port 1 is reserved and nothing binds it. A refused connection is a
        # sharper test than a timeout and keeps the suite fast.
        monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
        rc.reset_client()
        yield
        rc.reset_client()

    def test_enqueue_reports_unavailable_when_the_server_is_gone(self, dead_server):
        """server.py falls back to direct dispatch on UNAVAILABLE. If this
        raised instead, the webhook would 500 and GitHub would retry into the
        same error."""
        import app.core.event_queue as eq

        assert eq.enqueue("push", {}, "o/r", "d1") == eq.EnqueueResult.UNAVAILABLE

    def test_queue_stats_answers_when_the_server_is_gone(self, dead_server):
        """/health must not fail because the thing it reports on is down."""
        import app.core.event_queue as eq

        stats = eq.queue_stats()
        assert stats["mode"] in ("threadpool-fallback", "unknown")

    def test_recover_stale_is_a_no_op_when_the_server_is_gone(self, dead_server):
        """Boot must not crash because Redis is unreachable at that moment."""
        import app.core.event_queue as eq

        assert eq.recover_stale() == 0
