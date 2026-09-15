"""
app/core/process_guard.py — detect a multi-process web deployment.

WHY THIS EXISTS
  `--workers 1` is load-bearing and nothing enforced it. Dockerfile, Procfile
  and render.yaml all set it, each with a comment explaining why, and every one
  of those comments is invisible to the person who raises the number because
  traffic grew.

  Three things in this application are process-wide singletons that assume one
  web process:

    - The durable queue's consumer group. Each process starts
      EVENT_QUEUE_CONSUMERS threads, so N workers means N times the consumers.
      They compete for the same list, which is safe — BLMOVE is atomic — but
      recover_stale() at boot is not: it assumes nothing is legitimately
      in-flight and requeues everything it finds in evq:processing. A second
      process booting while the first is mid-handler requeues that event, and
      it gets handled twice.

    - The in-memory rate-limit fallbacks. The per-user command limit and the
      per-IP webhook limit both fall back to process-local dicts when Redis is
      unavailable, so N workers means N times the allowance.

    - The bounded thread pool. Its saturation backpressure is what turns a
      flood into a 503 rather than an out-of-memory kill; N pools means N times
      the memory ceiling on a 512MB instance.

  None of that is a crash. It is a slow, confusing wrongness — duplicated
  comments, a rate limit that does not hold, memory that grows past where it
  was meant to stop. Exactly the kind of failure that takes a day to trace back
  to a one-character configuration change.

HOW IT DETECTS
  There is no reliable way to ask gunicorn how many workers it spawned from
  inside a worker, so this counts them instead. Each process registers its PID
  in Redis at boot under a short-lived key. More than one distinct PID inside
  the window means more than one web process.

  Redis-based because that is the only thing the processes share. With no
  Redis the check reports "unknown" and says so, rather than guessing — and a
  deployment with no Redis has already lost durability, which is the louder
  problem.
"""

from __future__ import annotations

import logging
import os
import time

log = logging.getLogger(__name__)

# Long enough that every worker in a rolling start registers inside it, short
# enough that a previous deploy's PIDs have expired before the next one looks.
REGISTRATION_TTL_SECONDS = 90

_KEY_PREFIX = "web:proc:"


def register_this_process() -> None:
    """Record this PID at boot. Never raises — a diagnostic must not stop boot."""
    try:
        from app.core.redis_client import get_redis, is_redis_available

        if not is_redis_available():
            return
        get_redis().set(
            f"{_KEY_PREFIX}{os.getpid()}",
            str(int(time.time())),
            ex=REGISTRATION_TTL_SECONDS,
        )
    except Exception as exc:
        log.debug(f"process_guard.register_skipped: {exc}")


def active_process_count() -> int:
    """
    How many web processes registered recently. 0 means "could not tell".

    Counted with SCAN rather than KEYS: KEYS blocks the server for the whole
    keyspace, and this runs on the /health path.
    """
    try:
        from app.core.redis_client import get_redis, is_redis_available

        if not is_redis_available():
            return 0
        r = get_redis()
        seen = set()
        for key in r.scan_iter(match=f"{_KEY_PREFIX}*", count=100):
            seen.add(key.decode() if isinstance(key, bytes) else key)
            if len(seen) > 32:  # a bound; the answer is already "too many"
                break
        return len(seen)
    except Exception as exc:
        log.debug(f"process_guard.count_failed: {exc}")
        return 0


def verdict() -> tuple[str, str]:
    """
    ("ok" | "warn" | "unknown", explanation) — the shape preflight reports in.
    """
    count = active_process_count()

    if count == 0:
        return (
            "unknown",
            "Could not determine how many web processes are running, because "
            "Redis is unavailable. Keep gunicorn at --workers 1: the queue "
            "consumers, the rate-limit fallbacks and the thread pool are all "
            "process-wide singletons.",
        )

    if count == 1:
        return ("ok", "One web process, which is what the singletons in this app assume.")

    consumers = os.environ.get("EVENT_QUEUE_CONSUMERS", "2")
    return (
        "warn",
        f"{count} web processes are running. This app is built for one. "
        f"Each starts its own queue consumers (EVENT_QUEUE_CONSUMERS={consumers}) "
        "and runs recover_stale() at boot, which requeues anything it finds "
        "in-flight — so an event another process is handling right now can be "
        "processed twice. The per-user and per-IP rate limits also fall back to "
        "process-local counters, so the effective limit is multiplied. "
        "Set gunicorn to --workers 1. To scale out, run worker.py as its own "
        "service and set EVENT_QUEUE_CONSUMERS=0 on the web process.",
    )


def warn_if_multiprocess() -> None:
    """Called at boot. Logs loudly rather than refusing to start.

    Refusing would turn a performance misconfiguration into an outage, and the
    operator who set --workers 4 did so because the service was struggling.
    """
    state, message = verdict()
    if state == "warn":
        log.error(f"process_guard.multiple_web_processes — {message}")
