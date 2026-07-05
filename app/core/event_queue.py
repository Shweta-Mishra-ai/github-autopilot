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
    server.py returns 503 so GitHub retries later. Nothing grows unbounded.
  - Envelope size capped (MAX_ENVELOPE_BYTES): giant payloads skip Redis and
    fall back to direct thread-pool dispatch instead of filling Redis.
  - Dead-letter list trimmed to DEAD_MAX entries.
  - No in-process buffering: consumers hold at most one event each.

FAILURE MODES (all explicit, all observable via metrics)
  Redis down       → enqueue() returns UNAVAILABLE → caller falls back to
                     direct thread-pool dispatch (degraded, not broken).
  Queue full       → FULL → 503 → GitHub redelivers (up to ~1h of retries).
  Handler crashes  → event requeued once (attempts+1), then dead-lettered.
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

import json
import logging
import os
import threading
import time
from typing import Callable

from app.core.metrics import metrics
from app.core.redis_client import get_redis, get_redis_blocking, is_redis_available

log = logging.getLogger(__name__)

# ── Keys & limits ─────────────────────────────────────────────────────────────

PENDING_KEY = "evq:pending"
PROCESSING_KEY = "evq:processing"
DEAD_KEY = "evq:dead"

MAX_QUEUE_LEN = int(os.environ.get("EVENT_QUEUE_MAX_LEN", "200"))
MAX_ENVELOPE_BYTES = int(os.environ.get("EVENT_QUEUE_MAX_ITEM_BYTES", str(512 * 1024)))
MAX_ATTEMPTS = 2  # first run + one retry, then dead-letter
DEAD_MAX = 50  # keep at most 50 dead envelopes for debugging
CONSUMER_COUNT = int(os.environ.get("EVENT_QUEUE_CONSUMERS", "2"))
BLOCK_SECONDS = 5  # BLMOVE timeout — also the shutdown responsiveness bound

# ── Enqueue results (sentinels, not exceptions) ───────────────────────────────


class EnqueueResult:
    OK = "ok"
    FULL = "full"
    UNAVAILABLE = "unavailable"
    TOO_LARGE = "too_large"


def enqueue(event: str, payload: dict, repo: str, delivery_id: str = "") -> str:
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
            log.error(f"queue.full len>={MAX_QUEUE_LEN} — 503 so GitHub retries")
            return EnqueueResult.FULL
        r.lpush(PENDING_KEY, envelope)
        metrics.increment("queue.enqueued")
        return EnqueueResult.OK
    except Exception as e:
        log.warning(f"queue.enqueue_failed: {e} — falling back to direct dispatch")
        metrics.increment("queue.enqueue_failed")
        return EnqueueResult.UNAVAILABLE


# ── Crash recovery ────────────────────────────────────────────────────────────


def recover_stale(max_items: int = MAX_QUEUE_LEN) -> int:
    """
    Requeue events stranded in evq:processing by a crash/restart.
    Single-consumer-process design: at boot nothing is legitimately
    in-flight, so everything found there is a leftover.
    Items that already used their attempts go to evq:dead.
    Returns number of items recovered.
    """
    if not is_redis_available():
        return 0

    recovered = 0
    try:
        r = get_redis()
        for _ in range(max_items):
            raw = r.rpop(PROCESSING_KEY)
            if raw is None:
                break
            try:
                env = json.loads(raw)
                env["attempts"] = int(env.get("attempts", 0)) + 1
            except Exception:
                metrics.increment("queue.dead")
                r.lpush(DEAD_KEY, raw)
                r.ltrim(DEAD_KEY, 0, DEAD_MAX - 1)
                continue

            if env["attempts"] >= MAX_ATTEMPTS:
                metrics.increment("queue.dead")
                r.lpush(DEAD_KEY, json.dumps(env, separators=(",", ":")))
                r.ltrim(DEAD_KEY, 0, DEAD_MAX - 1)
                log.warning(f"queue.dead_letter id={env.get('id')} event={env.get('event')}")
            else:
                metrics.increment("queue.requeued")
                r.lpush(PENDING_KEY, json.dumps(env, separators=(",", ":")))
                recovered += 1
        if recovered:
            log.info(f"queue.recovered_stale count={recovered}")
    except Exception as e:
        log.error(f"queue.recover_failed: {e}")
    return recovered


# ── Consumer group ────────────────────────────────────────────────────────────

_stop = threading.Event()
_threads: list[threading.Thread] = []
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
        env = json.loads(raw)
        handler(env.get("event", ""), env.get("payload") or {}, env.get("repo", "unknown"))
        metrics.increment("queue.consumed")
    finally:
        # Remove from processing even if handler raised: _run_handler swallows
        # everything except truly fatal errors, and a fatal error would be
        # retried forever otherwise.
        r.lrem(PROCESSING_KEY, 1, raw)
    return True


def _consume_loop(handler: Callable[[str, dict, str], None], worker_id: int) -> None:
    """
    One consumer thread. handler must never raise for expected errors
    (server._run_handler already guarantees that); if it does raise, the
    envelope is dropped from processing by _consume_once's finally block.
    """
    log.info(f"queue.consumer_started id={worker_id}")
    while not _stop.is_set():
        try:
            _consume_once(handler)
        except Exception as e:
            if not _stop.is_set():
                log.error(f"queue.consumer_error id={worker_id}: {e}")
                metrics.increment("queue.consumer_error")
                time.sleep(1)  # avoid hot-looping when Redis is down
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
        log.warning("queue.no_redis — consumers not started, thread-pool fallback active")
        return 0

    with _threads_lock:
        if _threads:
            return len(_threads)
        _stop.clear()
        recover_stale()
        for i in range(CONSUMER_COUNT):
            t = threading.Thread(
                target=_consume_loop, args=(handler, i), daemon=True, name=f"evq-consumer-{i}"
            )
            t.start()
            _threads.append(t)
        log.info(f"queue.consumers_started count={len(_threads)}")
        return len(_threads)


def stop_consumers(timeout: float = 10.0) -> None:
    """Graceful drain: signal stop, join with bound. Called from SIGTERM."""
    _stop.set()
    with _threads_lock:
        for t in _threads:
            t.join(timeout=timeout / max(len(_threads), 1))
        _threads.clear()
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
