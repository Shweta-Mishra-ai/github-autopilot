"""
app/core/thread_pool.py
  1. SILENT DROP BUG: When pool was saturated, webhook returned 202 (Accepted).
     GitHub treats 202 as success and does NOT retry. Dropped events were
     permanently lost with no recovery path.

     Fix: dispatch() now returns a SaturatedError sentinel so server.py can
     return 503, which causes GitHub to retry automatically (up to 3 attempts
     over ~1 hour). Use is_saturated_error() to check.

  2. Gunicorn + ThreadPool interaction documented clearly.
     This app runs gunicorn --workers 1 --threads 8. The single worker process
     means the ThreadPoolExecutor singleton is truly shared. workers=1 is
     intentional and MUST NOT be changed without re-evaluating pool sizing.

Thread model:
  - Gunicorn gthread: 8 threads handle HTTP (fast, non-blocking — just ACK)
  - ThreadPoolExecutor: 6 workers do actual AI/GitHub work (slow, IO-bound)
  - Both live in the same process → singleton is safe.
  - If workers > 1 is ever needed: move to Redis Queue (see archive/).
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Callable

import os

log = logging.getLogger(__name__)

# ── Saturation sentinel ───────────────────────────────────────────────────────


class _SaturatedError:
    """Returned by dispatch() when queue is full. Not an exception."""

    pass


_SATURATED = _SaturatedError()


def is_saturated(result) -> bool:
    """True if dispatch() returned the saturation sentinel."""
    return isinstance(result, _SaturatedError)


# ── Bounded thread pool ───────────────────────────────────────────────────────

# On Render free tier (512MB RAM, 0.5 CPU), >10 concurrent LLM calls
# will hit OOM or timeout. Keep conservative.
# gunicorn runs --workers 1, so this singleton is process-wide and safe.
MAX_DISPATCH_WORKERS = int(os.environ.get("MAX_DISPATCH_WORKERS", "6"))
_QUEUE_MAXSIZE = 50  # Pending work items; beyond this → 503 to GitHub

_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()
_pending: int = 0
_pending_lock = threading.Lock()


def get_pool() -> ThreadPoolExecutor:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ThreadPoolExecutor(
                    max_workers=MAX_DISPATCH_WORKERS,
                    thread_name_prefix="webhook-dispatch",
                )
                log.info(f"thread_pool.created max_workers={MAX_DISPATCH_WORKERS}")
    return _pool


def dispatch(fn: Callable, *args, **kwargs) -> "Future | _SaturatedError":
    """
    Submit fn(*args, **kwargs) to the bounded pool.

    Returns:
      - Future   on success (event is being processed)
      - _SATURATED if pool queue is full — caller MUST return 503 to GitHub
                  so GitHub retries automatically.

    Never raises. Caller must use is_saturated() to check return value.
    """
    global _pending

    with _pending_lock:
        current = _pending
        if current >= _QUEUE_MAXSIZE:
            log.error(
                f"thread_pool.queue_full pending={current} "
                f"max={_QUEUE_MAXSIZE} — signalling 503 for GitHub retry"
            )
            return _SATURATED
        _pending += 1

    def _wrapped():
        try:
            fn(*args, **kwargs)
        except Exception as e:
            log.error(f"thread_pool.worker_error: {e}", exc_info=True)
        finally:
            global _pending
            with _pending_lock:
                _pending -= 1

    try:
        future = get_pool().submit(_wrapped)
        return future
    except Exception as e:
        log.error(f"thread_pool.submit_error: {e}")
        with _pending_lock:
            _pending -= 1
        return _SATURATED


def pool_stats() -> dict:
    """Returns current pool stats for health endpoint."""
    get_pool()  # ensure pool is initialised
    with _pending_lock:
        pend = _pending
    return {
        "max_workers": MAX_DISPATCH_WORKERS,
        "pending_tasks": pend,
        "queue_capacity": _QUEUE_MAXSIZE,
        "saturation_pct": round(pend / _QUEUE_MAXSIZE * 100, 1),
    }


def shutdown(wait: bool = True):
    """Graceful shutdown. Call on SIGTERM."""
    global _pool
    if _pool:
        log.info(f"thread_pool.shutdown wait={wait}")
        _pool.shutdown(wait=wait)
        _pool = None


# ── Thread-safe config cache lock ─────────────────────────────────────────────
# Used in app/core/config.py

config_cache_lock = threading.RLock()
