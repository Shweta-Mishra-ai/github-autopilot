"""
Metrics - app/core/metrics.py
Simple in-memory counters exposed via /metrics endpoint.
No external dependency needed.

Usage:
    from app.core.metrics import metrics
    metrics.increment("events.issues.processed")
    metrics.increment("ai.calls.total")
    metrics.snapshot()  → dict of all counters
"""

import time
import threading
from collections import defaultdict


class MetricsCollector:
    def __init__(self):
        self._counters: dict = defaultdict(int)
        self._lock = threading.Lock()
        self._start_time = time.time()

    def increment(self, key: str, value: int = 1):
        """Increment a counter by value (default 1)."""
        with self._lock:
            self._counters[key] += value

    def get(self, key: str, default: int = 0) -> int:
        with self._lock:
            return self._counters.get(key, default)

    def snapshot(self) -> dict:
        """Return all metrics as a dict — safe to serialize to JSON."""
        with self._lock:
            uptime_seconds = int(time.time() - self._start_time)
            return {
                "uptime_seconds": uptime_seconds,
                "uptime_human": _format_uptime(uptime_seconds),
                **dict(self._counters)
            }

    def reset(self):
        """Reset all counters — useful for testing."""
        with self._lock:
            self._counters.clear()
            self._start_time = time.time()


def _format_uptime(seconds: int) -> str:
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours}h {minutes}m {secs}s"


# Singleton — import this everywhere
metrics = MetricsCollector()

