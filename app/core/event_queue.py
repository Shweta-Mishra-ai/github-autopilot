"""
app/core/event_queue.py — Durable Redis event queue with embedded consumers.

WHY THIS EXISTS
  ThreadPoolExecutor alone loses every queued event on restart/deploy/crash:
  the pending work lives only in process memory. This module makes webhook
  processing durable by parking events in Redis first, then consuming them
  with a small in-process worker group.

ARCHITECTURE (free tier today, scales tomorrow)
  Producer:  server.py /webhook → enqueue() → LPUSH evq:pending  → 202
  Consumer:  N daemon threads   → BLMOVE evq:pending → evq:processing
             → handler() → LREM evq:processing (at-least-once semantics)
  Recovery:  on boot, everything left in evq:processing (crash leftovers)
             is requeued to evq:pending; items that already failed twice go
             to evq:dead instead of looping forever.

  The consumer group runs INSIDE the web process because Render's free tier
  has no free background worker. Moving to a paid tier later needs zero code
  changes: run `python worker.py` as a Render worker service and stop calling
  start_consumers() in the web process (EVENT_QUEUE_CONSUMERS=0). The
  producer, envelope format, and recovery logic stay identical.

MEMORY SAFETY (free tier: 512MB app / 25MB Redis)
  - Queue length capped (MAX_QUEUE_LEN): beyond it enqueue() reports FULL and
    server.py returns 503. GitHub does not retry that by itself;
    app/github/redelivery.py asks it to. Nothing grows unbounded.
  - Envelope size capped (MAX_ENVELOPE_BYTES): giant payloads skip Redis and
    fall back to direct thread-pool dispatch instead of filling Redis.
  - Dead-letter list trimmed to DEAD_MAX entries.
  - No in-process buffering: consumers hold at most one event each.

FAILURE MODES (all explicit, all observable via metrics)
  Redis down       → enqueue() returns UNAVAILABLE → caller falls back to
                     direct thread-pool dispatch (degraded, not broken).
  Queue full       → FULL → 503. GitHub does NOT redeliver on its own; the
                     dedup key is released so a manual redelivery runs.
  Transient error  → handler returns RETRY → retried after 30s, 5min and 30min
                     (MAX_ATTEMPTS=4 in all), then dead-lettered.
  Handler failure  → handler returns FAILED → dead-lettered at once.
  Process killed   → events in evq:processing requeued on next boot.

FIXED: consumers were logging a spurious "queue.consumer_error ... Timeout
  reading from socket" on nearly every idle poll cycle in production. Root
  cause: BLMOVE's own blocking wait (BLOCK_SECONDS=5) was racing the shared
  Redis client's socket read timeout (also 5s, in redis_client.py) — when the
  server legitimately blocked for the full 5s waiting for new work, the
  client's own read timeout fired at essentially the same instant, so nearly
  every idle poll raised a false error. _consume_once() now uses
  get_redis_blocking() (redis_client.py), a dedicated connection with a
  generous 30s socket timeout, for exactly this reason. Fast (non-blocking)
  operations elsewhere keep using get_redis()'s tight 5s timeout so they still
  fail fast if Redis is genuinely down.
"""

from __future__ import annotations

from app.core.env import env_int
import json
import logging
import threading
import time
import uuid
from typing import Callable

from app.core.metrics import metrics

# get_redis_blocking() (not get_redis()) is required for the BLMOVE call in
# _consume_once() -- see redis_client.BLOCKING_SOCKET_TIMEOUT for why.
from app.core.redis_client import get_redis, get_redis_blocking, is_redis_available

log = logging.getLogger(__name__)

# ── Keys & limits ─────────────────────────────────────────────────────────────

PENDING_KEY = "evq:pending"
PROCESSING_KEY = "evq:processing"
DEAD_KEY = "evq:dead"
# Who holds each in-flight envelope: {sha1(envelope): {"pid", "at"}}.
CLAIMS_KEY = "evq:claims"
# Retries waiting for their time: ZSET envelope -> due unix time.
DELAYED_KEY = "evq:delayed"
# A process is alive while its key exists; consumers refresh it.
ALIVE_KEY = "evq:alive:{}"

MAX_QUEUE_LEN = env_int("EVENT_QUEUE_MAX_LEN", 200, minimum=1)
MAX_ENVELOPE_BYTES = env_int("EVENT_QUEUE_MAX_ITEM_BYTES", 512 * 1024, minimum=1024)
# First run + three retries, then dead-letter. Two attempts 30 seconds apart
# dead-lettered every event of a GitHub rate-limit outage, which lasts up to
# an hour; the delays now grow.
MAX_ATTEMPTS = 4
RETRY_DELAYS = (30, 300, 1800)
RETRY_DELAY_SECONDS = RETRY_DELAYS[0]  # kept for callers and tests

# This process. A claim names it; its heartbeat says it is still working.
PROCESS_ID = uuid.uuid4().hex[:12]
HEARTBEAT_SECONDS = 10
ALIVE_TTL = 45
# A claim older than this is recovered even if its owner is alive: a handler
# that has not finished in half an hour is stuck, not slow.
LEASE_SECONDS = 1800
# An envelope found in processing with no claim (the owner died between the
# move and the claim) is recovered after this long.
UNCLAIMED_GRACE = 60


class HandlerOutcome:
    """What a handler reports back to the consumer. None counts as OK."""

    OK = "ok"
    RETRY = "retry"  # transient: retried with growing delays, then dead-lettered
    FAILED = "failed"  # not transient: dead-letter now, where it can be seen


DEAD_MAX = 50  # keep at most 50 dead envelopes for debugging
CONSUMER_COUNT = env_int("EVENT_QUEUE_CONSUMERS", 2, minimum=0)
BLOCK_SECONDS = 5  # BLMOVE timeout — also the shutdown responsiveness bound

# ── Enqueue results (sentinels, not exceptions) ───────────────────────────────


class EnqueueResult:
    OK = "ok"
    FULL = "full"
    UNAVAILABLE = "unavailable"
    TOO_LARGE = "too_large"


def enqueue(
    event: str, payload: dict, repo: str, delivery_id: str = "", fingerprint: str = ""
) -> str:
    """
    Park one webhook event in Redis. Returns an EnqueueResult constant.
    Never raises — Redis errors degrade to UNAVAILABLE so the caller can
    fall back to direct dispatch.
    """
    if not is_redis_available():
        return EnqueueResult.UNAVAILABLE

    envelope = json.dumps(
        {
            "id": delivery_id or f"noid-{int(time.time() * 1000)}",
            "event": event,
            "repo": repo,
            "payload": payload,
            "attempts": 0,
            "enqueued_at": int(time.time()),
            # The dedupe key, released if the event is dead-lettered so a
            # redelivery from GitHub's UI is processed instead of skipped.
            "fp": fingerprint,
        },
        separators=(",", ":"),
    )
    if len(envelope.encode("utf-8", errors="ignore")) > MAX_ENVELOPE_BYTES:
        metrics.increment("queue.too_large")
        return EnqueueResult.TOO_LARGE

    try:
        r = get_redis()
        if int(r.llen(PENDING_KEY) or 0) >= MAX_QUEUE_LEN:
            metrics.increment("queue.full")
            log.error(
                f"queue.full len>={MAX_QUEUE_LEN} — 503; the redelivery sweep will ask GitHub to resend it"
            )
            return EnqueueResult.FULL
        r.lpush(PENDING_KEY, envelope)
        metrics.increment("queue.enqueued")
        return EnqueueResult.OK
    except Exception as e:
        log.warning(f"queue.enqueue_failed: {e} — falling back to direct dispatch")
        metrics.increment("queue.enqueue_failed")
        return EnqueueResult.UNAVAILABLE


def schedule(event: str, payload: dict, repo: str, delay: float, job_id: str) -> bool:
    """
    Park an internal job to run after `delay` seconds, with the same retries
    as a webhook event. False when Redis is unavailable — the caller decides
    what losing it means. Never raises.
    """
    if not is_redis_available():
        return False
    envelope = json.dumps(
        {
            "id": job_id,
            "event": event,
            "repo": repo,
            "payload": payload,
            "attempts": 0,
            "enqueued_at": int(time.time()),
            "fp": "",
        },
        separators=(",", ":"),
    )
    try:
        get_redis().zadd(DELAYED_KEY, {envelope: time.time() + delay})
        metrics.increment("queue.scheduled")
        return True
    except Exception as e:
        log.warning(f"queue.schedule_failed id={job_id}: {e}")
        return False


# ── Crash recovery ────────────────────────────────────────────────────────────


def _claim_id(raw: str) -> str:
    import hashlib

    # An identity key for the claims hash, not a security boundary.
    return hashlib.sha1(raw.encode("utf-8", "surrogatepass"), usedforsecurity=False).hexdigest()


def _claim(r, raw: str) -> None:
    r.hset(CLAIMS_KEY, _claim_id(raw), json.dumps({"pid": PROCESS_ID, "at": int(time.time())}))


def _alive(r, pid: str) -> bool:
    return pid == PROCESS_ID or bool(r.exists(ALIVE_KEY.format(pid)))


def _stale(r, raw: str, now: float) -> bool:
    """Whether an in-flight envelope belongs to nobody who is still working."""
    cid = _claim_id(raw)
    claim = r.hget(CLAIMS_KEY, cid)
    if claim is None:
        # Moved but never claimed: the owner died in between. Give a live
        # owner a moment to write its claim before calling it abandoned.
        # A placeholder with no owner; it is stale once it outlives the grace
        # period. A consumer that claims the envelope overwrites it.
        r.hsetnx(CLAIMS_KEY, cid, json.dumps({"pid": "", "at": int(now)}))
        return False
    try:
        c = json.loads(claim)
    except Exception:
        return True
    pid, at = str(c.get("pid") or ""), int(c.get("at") or 0)
    if not pid:
        return now - at > UNCLAIMED_GRACE
    if now - at > LEASE_SECONDS:
        return True
    return not _alive(r, pid)


def recover_stale(max_items: int = MAX_QUEUE_LEN) -> int:
    """
    Requeue in-flight events whose owner is gone; returns how many.

    This ran at boot and assumed nothing was legitimately in flight, so it
    moved EVERYTHING in evq:processing back to pending. That is false whenever
    two processes overlap — a zero-downtime deploy boots the new instance
    while the old one is still handling events, and several worker.py
    replicas are the documented way to scale — so an event being handled was
    requeued and handled twice (duplicate comments, PRs, merges), and what the
    old process still held when it was killed stayed stuck until some later
    boot. Each in-flight envelope is now claimed by the process that took it,
    every process keeps a heartbeat, and only an envelope whose owner's
    heartbeat has lapsed (or whose lease ran out) is recovered — by whichever
    process notices first, every minute rather than once at boot. LREM is the
    arbiter: the one recovery that removes the envelope is the one that
    requeues it.
    """
    if not is_redis_available():
        return 0

    recovered = 0
    try:
        r = get_redis()
        now = time.time()
        for raw in r.lrange(PROCESSING_KEY, 0, max_items - 1) or []:
            if not _stale(r, raw, now):
                continue
            if not r.lrem(PROCESSING_KEY, 1, raw):
                continue  # another process recovered it first
            r.hdel(CLAIMS_KEY, _claim_id(raw))
            try:
                env = json.loads(raw)
                env["attempts"] = int(env.get("attempts", 0)) + 1
            except Exception:
                metrics.increment("queue.dead")
                r.lpush(
                    DEAD_KEY,
                    json.dumps({"why": "unreadable envelope", "bytes": len(raw or "")}),
                )
                r.ltrim(DEAD_KEY, 0, DEAD_MAX - 1)
                continue

            if env["attempts"] >= MAX_ATTEMPTS:
                _dead_letter(r, env, "owner stopped while handling it, attempts exhausted")
            else:
                metrics.increment("queue.requeued")
                r.lpush(PENDING_KEY, json.dumps(env, separators=(",", ":")))
                recovered += 1
        if recovered:
            log.info(f"queue.recovered_stale count={recovered}")
    except Exception as e:
        log.error(f"queue.recover_failed: {e}")
    return recovered


def promote_due(limit: int = 50) -> int:
    """Move retries whose delay has passed from the delayed set to pending."""
    try:
        r = get_redis()
        moved = 0
        for raw in r.zrangebyscore(DELAYED_KEY, 0, time.time(), start=0, num=limit) or []:
            if r.zrem(DELAYED_KEY, raw):  # one process wins each item
                r.lpush(PENDING_KEY, raw)
                moved += 1
        return moved
    except Exception as e:
        log.warning(f"queue.promote_failed: {e}")
        return 0


def _housekeeping_loop() -> None:
    """Heartbeat, due retries, and recovery of abandoned work."""
    last_recover = 0.0
    while not _stop.is_set():
        try:
            get_redis().set(ALIVE_KEY.format(PROCESS_ID), "1", ex=ALIVE_TTL)
            promote_due()
            if time.time() - last_recover >= 60:
                recover_stale()
                last_recover = time.time()
        except Exception as e:
            log.warning(f"queue.housekeeping_failed: {e}")
        _stop.wait(HEARTBEAT_SECONDS)


# ── Consumer group ────────────────────────────────────────────────────────────

_stop = threading.Event()
_threads: list[threading.Thread] = []
_housekeeper: threading.Thread | None = None
_threads_lock = threading.Lock()


def _consume_once(handler: Callable[[str, dict, str], None]) -> bool:
    """
    One consume step: BLMOVE pending→processing, run handler, LREM processing.
    Returns True if an event was processed. Split out of the loop for tests.

    Uses get_redis_blocking() (generous socket timeout) rather than get_redis()
    (tight 5s timeout) for the BLMOVE call: a client socket timeout equal to or
    below BLOCK_SECONDS races the server's own blocking wait and raises a
    spurious "Timeout reading from socket" error on nearly every idle poll —
    see redis_client.BLOCKING_SOCKET_TIMEOUT for the full explanation.
    """
    r = get_redis_blocking()
    # LPUSH producer + RIGHT-pop consumer = FIFO ordering
    raw = r.blmove(PENDING_KEY, PROCESSING_KEY, timeout=BLOCK_SECONDS, src="RIGHT", dest="LEFT")
    if raw is None:
        return False
    try:
        _claim(get_redis(), raw)
    except Exception as e:
        log.warning(f"queue.claim_failed: {e}")
    try:
        env = json.loads(raw)
        outcome = handler(
            env.get("event", ""), env.get("payload") or {}, env.get("repo", "unknown")
        )
        _settle(r, env, outcome)
    finally:
        # Remove from processing even if handler raised: a fatal error would
        # be retried forever otherwise.
        r.lrem(PROCESSING_KEY, 1, raw)
        try:
            get_redis().hdel(CLAIMS_KEY, _claim_id(raw))
        except Exception as e:
            log.debug(f"queue.unclaim_failed: {e}")
    return True


def _dead_letter(r, env: dict, why: str) -> None:
    metrics.increment("queue.dead")
    # What identifies the event and why it failed — not its payload. Nothing
    # replays a dead letter (GitHub's "Redeliver" does, by delivery id), and
    # 50 payloads of up to 512 KB came to 26 MB: more than the free Key Value
    # plan holds, with no expiry.
    record = {k: env.get(k) for k in ("id", "event", "repo", "attempts", "enqueued_at")}
    r.lpush(DEAD_KEY, json.dumps({**record, "why": why[:200]}, separators=(",", ":")))
    r.ltrim(DEAD_KEY, 0, DEAD_MAX - 1)
    log.warning(f"queue.dead_letter id={env.get('id')} event={env.get('event')} why={why[:120]}")
    # The dedupe key stayed set for 24h, so GitHub's "Redeliver" — the only
    # way to recover a dead-lettered event — was answered "duplicate".
    if env.get("fp"):
        from app.core.idempotency import forget

        forget(str(env["fp"]))


def _settle(r, env: dict, outcome) -> None:
    """
    Act on a handler's outcome.

    The envelope used to be removed whatever happened: the handler swallows
    every exception, so a transient GitHub 5xx, a secondary rate limit or a
    provider outage dropped the event for good — counted as `queue.consumed`,
    logged as "GitHub will retry", when GitHub had already been given a 202
    and never would. A transient failure is now requeued once after a short
    wait; anything else goes to the dead-letter list, where it can be seen.
    """
    if outcome in (None, HandlerOutcome.OK):
        metrics.increment("queue.consumed")
        return
    if outcome == HandlerOutcome.RETRY:
        env["attempts"] = int(env.get("attempts", 0)) + 1
        if env["attempts"] < MAX_ATTEMPTS:
            # Scheduled, not slept: waiting in the consumer held one of the
            # two consumer threads for 30s per failing event, so an outage
            # throttled the whole queue to a few events a minute.
            delay = RETRY_DELAYS[min(env["attempts"] - 1, len(RETRY_DELAYS) - 1)]
            metrics.increment("queue.retried")
            get_redis().zadd(
                DELAYED_KEY, {json.dumps(env, separators=(",", ":")): time.time() + delay}
            )
            log.info(
                f"queue.retry_scheduled id={env.get('id')} event={env.get('event')} in={delay}s"
            )
            return
        _dead_letter(r, env, "transient failure persisted after retry")
        return
    _dead_letter(r, env, "handler failed")


_last_consumer_error = 0.0


def consumer_failing(within_seconds: int = 300) -> bool:
    """True when a consumer failed within the last `within_seconds` — the
    queue is not draining now, as opposed to a blip earlier in the process."""
    return bool(_last_consumer_error) and time.time() - _last_consumer_error < within_seconds


def _consume_loop(handler: Callable[[str, dict, str], None], worker_id: int) -> None:
    """
    One consumer thread. handler must never raise for expected errors
    (server._run_handler already guarantees that); if it does raise, the
    envelope is dropped from processing by _consume_once's finally block.
    """
    log.info(f"queue.consumer_started id={worker_id}")
    failures = 0
    while not _stop.is_set():
        try:
            _consume_once(handler)
            failures = 0
        except Exception as e:
            if not _stop.is_set():
                failures += 1
                # Backed off: a Redis that is full (noeviction refuses BLMOVE)
                # or down fails every attempt, and a fixed 1 s retry wrote two
                # error lines a second for as long as it lasted.
                delay = min(60, 2 ** min(failures - 1, 6))
                log.error(
                    f"queue.consumer_error id={worker_id} attempt={failures} "
                    f"next_in={delay}s: {type(e).__name__}: {e}"
                )
                metrics.increment("queue.consumer_error")
                global _last_consumer_error
                _last_consumer_error = time.time()
                _stop.wait(delay)
    log.info(f"queue.consumer_stopped id={worker_id}")


def start_consumers(handler: Callable[[str, dict, str], None]) -> int:
    """
    Start the consumer group (idempotent). Returns number of threads started.
    Skipped entirely when EVENT_QUEUE_CONSUMERS=0 (standalone worker mode)
    or Redis is unavailable (thread-pool fallback handles everything).
    """
    if CONSUMER_COUNT <= 0:
        log.info("queue.consumers_disabled (EVENT_QUEUE_CONSUMERS=0)")
        return 0
    if not is_redis_available():
        # Not "never": Redis that was unreachable at boot (still starting on a
        # deploy) used to leave the queue without consumers for the life of
        # the process. A watcher starts them once Redis answers.
        log.warning("queue.no_redis — thread-pool fallback until Redis is reachable")
        _start_when_redis_returns(handler)
        return 0

    with _threads_lock:
        if _threads:
            return len(_threads)
        _stop.clear()
        try:
            get_redis().set(ALIVE_KEY.format(PROCESS_ID), "1", ex=ALIVE_TTL)
        except Exception as e:
            log.warning(f"queue.heartbeat_failed: {e}")
        recover_stale()
        global _housekeeper
        _housekeeper = threading.Thread(
            target=_housekeeping_loop, daemon=True, name="evq-housekeeping"
        )
        _housekeeper.start()
        for i in range(CONSUMER_COUNT):
            t = threading.Thread(
                target=_consume_loop, args=(handler, i), daemon=True, name=f"evq-consumer-{i}"
            )
            t.start()
            _threads.append(t)
        log.info(f"queue.consumers_started count={len(_threads)}")
        return len(_threads)


_watcher: threading.Thread | None = None
REDIS_WATCH_SECONDS = 15


def _start_when_redis_returns(handler: Callable[[str, dict, str], None]) -> None:
    global _watcher
    from app.core.redis_client import REDIS_URL

    if not (REDIS_URL or __import__("os").environ.get("REDIS_URL")):
        return  # no Redis configured at all: the fallback is the design
    if _watcher is not None and _watcher.is_alive():
        return

    def _watch() -> None:
        while not _stop.wait(REDIS_WATCH_SECONDS):
            if is_redis_available():
                log.info("queue.redis_back — starting consumers")
                start_consumers(handler)
                return

    _watcher = threading.Thread(target=_watch, daemon=True, name="evq-redis-watch")
    _watcher.start()


def stop_consumers(timeout: float = 10.0) -> None:
    """Graceful drain: signal stop, join with bound. Called from SIGTERM."""
    _stop.set()
    with _threads_lock:
        for t in _threads:
            t.join(timeout=timeout / max(len(_threads), 1))
        _threads.clear()
        if _housekeeper is not None:
            _housekeeper.join(timeout=1.0)
    log.info("queue.consumers_stopped")


def queue_stats() -> dict:
    """Depths for /health. Safe when Redis is down."""
    try:
        if not is_redis_available():
            return {"mode": "threadpool-fallback", "pending": 0, "processing": 0, "dead": 0}
        r = get_redis()
        return {
            "mode": "redis",
            "pending": int(r.llen(PENDING_KEY) or 0),
            "processing": int(r.llen(PROCESSING_KEY) or 0),
            "dead": int(r.llen(DEAD_KEY) or 0),
            "max_len": MAX_QUEUE_LEN,
            "consumers": len(_threads),
        }
    except Exception:
        return {"mode": "unknown", "pending": -1, "processing": -1, "dead": -1}
