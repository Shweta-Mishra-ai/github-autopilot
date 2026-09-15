"""
worker.py — Standalone event-queue consumer.

TODAY (Render free tier): NOT deployed. The web process runs the same
consumers in-process via server._boot() because free tier has no worker dyno.

FUTURE (paid tier / higher load): deploy this as a Render "worker" service and
set EVENT_QUEUE_CONSUMERS=0 on the web service. Zero code changes anywhere
else — producer, envelope format, and recovery logic are shared via
app/core/event_queue.py.

    startCommand: python worker.py

Env: same as web service (REDIS_URL, GITHUB_*, GROQ_API_KEY, ...).
"""

import logging
import os
import signal
import sys
import time

from app.core.logger import setup_logging

# Same configuration as the web process, for the same reasons — see the note
# in server.py. A worker whose LOG_LEVEL is ignored is the worse half of that
# bug: the web process at least answers /health, while this one is only ever
# debugged through its logs.
setup_logging(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    fmt=os.environ.get("LOG_FORMAT", "text"),
)
log = logging.getLogger("worker")


def run() -> None:
    from app.core.event_queue import start_consumers, stop_consumers
    from app.core.webhook_security import startup_check
    from server import _run_handler

    startup_check()

    # Maintenance (memory restore + the periodic sweep) is already running:
    # `from server import _run_handler` above imports server.py, whose module
    # scope calls _boot() when it is not __main__. Calling it again here would
    # read as a second requirement when it is the same one.
    started = start_consumers(_run_handler)
    if not started:
        log.error("worker.no_consumers — Redis unavailable or EVENT_QUEUE_CONSUMERS=0")
        sys.exit(1)

    def _sigterm(signum, frame):
        log.info("worker.sigterm — draining")
        stop_consumers()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sigterm)
    log.info(f"worker.running consumers={started} pid={os.getpid()}")

    while True:
        time.sleep(60)


if __name__ == "__main__":
    run()
