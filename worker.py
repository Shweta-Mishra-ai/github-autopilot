"""
worker.py — V3 Event Worker
Pulls events from queue and dispatches to handlers.
Run alongside server.py as a separate process.
"""

import logging
from app.core.logger import get_logger
from app.core.metrics import metrics
from app.queue.consumer import consume_events

log = get_logger(__name__)


def _dispatch(event_type: str, payload: dict):
    if event_type == "pull_request":
        from app.handlers.pull_request import handle
        handle(payload)
    elif event_type == "issues":
        from app.handlers.issues import handle
        handle(payload)
    elif event_type == "issue_comment":
        from app.handlers.comments import handle
        handle(payload)
    elif event_type == "push":
        from app.handlers.push import handle
        handle(payload)
    else:
        log.debug("unhandled_event", event=event_type)


def run():
    log.info("worker.started")
    for event_type, payload in consume_events():
        try:
            _dispatch(event_type, payload)
            metrics.increment(f"events.{event_type}.success")
        except Exception as e:
            log.error("worker.dispatch_failed", event=event_type, error=str(e))
            metrics.increment(f"events.{event_type}.error")


if __name__ == "__main__":
    run()

