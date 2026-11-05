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


def _claimed_by_a_dead_process(queue, live_redis, raw):
    """What a crash leaves: the envelope claimed by a process whose
    heartbeat key has expired (it was never set here)."""
    live_redis.hset(
        queue.CLAIMS_KEY, queue._claim_id(raw), json.dumps({"pid": "crashed-proc", "at": 0})
    )


class TestDeployOverlap:
    def test_an_event_another_live_process_is_handling_is_not_stolen(self, queue, live_redis):
        """A zero-downtime deploy boots the new instance while the old one is
        still handling events. Recovery requeued them, so they ran twice."""
        queue.enqueue("pull_request", {"n": 2}, "o/r", "d2")
        raw = live_redis.rpoplpush(queue.PENDING_KEY, queue.PROCESSING_KEY)
        live_redis.hset(
            queue.CLAIMS_KEY, queue._claim_id(raw), json.dumps({"pid": "old-instance", "at": int(time.time())})
        )
        live_redis.set(queue.ALIVE_KEY.format("old-instance"), "1", ex=30)

        assert queue.recover_stale() == 0
        assert live_redis.llen(queue.PROCESSING_KEY) == 1

        live_redis.delete(queue.ALIVE_KEY.format("old-instance"))  # it stopped
        assert queue.recover_stale() == 1


class TestAnEventSurvivesACrash:
    """The reason the queue exists. A thread pool loses everything on restart;
    this must not."""

    def test_an_in_flight_event_is_requeued_at_boot(self, queue, live_redis):
        queue.enqueue("pull_request", {"n": 1}, "o/r", "d1")

        # Simulate a crash mid-handler: the envelope has moved to processing
        # and the process dies before LREM.
        raw = live_redis.rpoplpush(queue.PENDING_KEY, queue.PROCESSING_KEY)
        assert raw is not None
        _claimed_by_a_dead_process(queue, live_redis, raw)
        assert live_redis.llen(queue.PROCESSING_KEY) == 1
        assert live_redis.llen(queue.PENDING_KEY) == 0

        assert queue.recover_stale() == 1
        assert live_redis.llen(queue.PENDING_KEY) == 1
        assert live_redis.llen(queue.PROCESSING_KEY) == 0

    def test_a_poison_event_dead_letters_instead_of_looping(self, queue, live_redis):
        """Without this it is requeued forever and the queue never drains."""
        env = json.dumps(
            {"id": "d1", "event": "push", "repo": "o/r", "payload": {}, "attempts": queue.MAX_ATTEMPTS - 1}
        )
        live_redis.lpush(queue.PROCESSING_KEY, env)
        _claimed_by_a_dead_process(queue, live_redis, env)

        assert queue.recover_stale() == 0
        assert live_redis.llen(queue.DEAD_KEY) == 1
        assert live_redis.llen(queue.PENDING_KEY) == 0

    def test_unparseable_json_is_dead_lettered_not_crashed_on(self, queue, live_redis):
        live_redis.lpush(queue.PROCESSING_KEY, "{not json at all")
        _claimed_by_a_dead_process(queue, live_redis, "{not json at all")
        queue.recover_stale()
        assert live_redis.llen(queue.DEAD_KEY) == 1

    def test_the_dead_letter_list_stays_bounded(self, queue, live_redis):
        """25MB of Redis. An unbounded debug list is a slow outage."""
        for i in range(queue.DEAD_MAX + 25):
            raw = json.dumps({"id": i, "attempts": 5})
            live_redis.lpush(queue.PROCESSING_KEY, raw)
            _claimed_by_a_dead_process(queue, live_redis, raw)
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

        # Two real deliveries differ in their bodies; the same signed body under
        # a new delivery id is a replay (see test_idempotency).
        a = idem.make_fingerprint("delivery-1", "push", {"after": "aaa"})
        b = idem.make_fingerprint("delivery-2", "push", {"after": "bbb"})
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


class TestAParkedReplyIsDeliveredByTheQueue:
    """A reply GitHub refused for a passing reason waits on the delayed set,
    is promoted when due, and is posted by the real runner — on its own, with
    the command that produced it never run again."""

    def test_park_promote_deliver(self, queue, live_redis, monkeypatch):
        from unittest.mock import MagicMock, patch

        # Importing server boots it; with REDIS_URL live that would start
        # background consumers that take the job before this test does.
        monkeypatch.setattr(queue, "CONSUMER_COUNT", 0)
        import server
        from app.github import deferred_reply
        from app.github.client import GitHubError

        post = MagicMock(side_effect=GitHubError("bad gateway", 502))
        deferred_reply.post_comment("o/r", 4, "tok", 9, "the answer", post=post)
        assert live_redis.llen(queue.PENDING_KEY) == 0, "parked, not run at once"
        assert queue.promote_due() == 0, "not due yet"

        (raw,) = live_redis.zrange(queue.DELAYED_KEY, 0, -1)
        live_redis.zadd(queue.DELAYED_KEY, {raw: time.time() - 1})  # its time has come
        assert queue.promote_due() == 1

        # The first POST's answer was lost, not necessarily the comment: the
        # delivery looks for its marker first, and finds none here.
        with patch("app.github.auth.get_installation_token", return_value="fresh"), \
             patch("app.github.client.gh_get", return_value=[]), \
             patch("app.github.deferred_reply.gh_post") as delivered:
            assert queue._consume_once(server._run_handler)
        (path, token, body), _ = delivered.call_args
        assert (path, token) == ("/repos/o/r/issues/4/comments", "fresh")
        assert body["body"].startswith("the answer\n<!-- autopilot-reply:")
        assert live_redis.llen(queue.PROCESSING_KEY) == 0


def test_a_reused_repository_name_is_cleared_on_a_real_server(live_redis):
    """app/core/repo_identity.py against a real server: SCAN patterns and
    bytes/str handling differ from the in-process fake."""
    from app.core import repo_identity
    from app.intelligence import memory

    repo_identity.ensure("o/r", 111)
    memory.remember("o/r", "old owner's decision", kind="decision")
    live_redis.set("snapshot:o/r:3", "{}")
    live_redis.set("analytics:o/r:cmd:fix:2026-10-15", "4")
    live_redis.set("analytics:o/r2:cmd:fix:2026-10-15", "1")

    assert repo_identity.ensure("o/r", 111) is False
    assert repo_identity.ensure("o/r", 222) is True
    assert live_redis.get("snapshot:o/r:3") is None
    assert live_redis.get("analytics:o/r:cmd:fix:2026-10-15") is None
    assert live_redis.lrange("mem:o/r", 0, -1) == []
    assert live_redis.get("analytics:o/r2:cmd:fix:2026-10-15") == "1"
    assert live_redis.get("repo_id:o/r") == "222"
