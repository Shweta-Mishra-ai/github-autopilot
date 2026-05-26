"""
app/core/thread_pool.py
────────────────────────
Bounded thread pool for webhook dispatch.

WHY: Original server.py spawned Thread() per webhook — unbounded.
A webhook flood (or GitHub retry storm) could exhaust system threads,
cause OOM, or starve gunicorn workers.

FIX: ThreadPoolExecutor with max_workers cap. When saturated, new
webhooks are queued (up to queue_maxsize). If queue is full, the
webhook is still ACK'd 202 but logged as dropped — better than crashing.

ALSO FIXES: Config cache race condition.
  _config_cache was a plain dict written from multiple threads.
  Now protected by threading.RLock (reentrant so load_config can call
  invalidate without deadlocking).
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Callable

log = logging.getLogger(__name__)

# ── Bounded thread pool ───────────────────────────────────────────────────────

# On Render free tier (512MB RAM, 0.5 CPU), >10 concurrent LLM calls
# will hit OOM or timeout. Keep conservative.
MAX_DISPATCH_WORKERS = int(__import__("os").environ.get("MAX_DISPATCH_WORKERS", "6"))
_QUEUE_MAXSIZE = 50   # Pending work items before we start logging drops

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


def dispatch(fn: Callable, *args, **kwargs) -> Future | None:
    """
    Submit fn(*args, **kwargs) to the bounded pool.

    Returns Future on success, None if pool is saturated (logged as drop).
    Never raises — webhook ACK must always be returned promptly.
    """
    global _pending

    with _pending_lock:
        current = _pending
        if current >= _QUEUE_MAXSIZE:
            log.error(
                f"thread_pool.queue_full pending={current} "
                f"max={_QUEUE_MAXSIZE} — dropping event"
            )
            return None
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
        return None


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
        log.info("thread_pool.shutdown wait={wait}")
        _pool.shutdown(wait=wait)
        _pool = None


# ── Thread-safe config cache lock ─────────────────────────────────────────────
# Used in app/core/config.py — import this lock there.

config_cache_lock = threading.RLock()
