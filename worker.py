"""
worker.py

NOTE: This worker is not active in the default deployment.
The web service (server.py) uses a ThreadPoolExecutor for
event dispatch. This file is kept for future use when scaling
beyond the free tier requires a dedicated worker process.

To activate: wire app/core/thread_pool or archive/tasks.py
and run this as a separate Render Background Worker service.
"""

import logging

log = logging.getLogger(__name__)


def run():
    log.warning(
        "worker.py is not active in this deployment. "
        "Events are dispatched via the thread pool in server.py. "
        "See archive/tasks.py for the Celery-based implementation."
    )


if __name__ == "__main__":
    run()
