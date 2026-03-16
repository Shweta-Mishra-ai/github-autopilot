"""
worker.py — V3 Event Worker + Scheduler
Pulls events from queue and dispatches to handlers.
Also runs scheduled maintenance tasks via APScheduler.
"""

import os
import threading
from app.core.logger import get_logger, setup_logging
from app.core.metrics import metrics
from app.queue.consumer import consume_events

setup_logging()
log = get_logger(__name__)

SCHEDULED_REPO = os.environ.get("SCHEDULED_REPO", "")
SCHEDULED_INSTALLATION_ID = int(os.environ.get("SCHEDULED_INSTALLATION_ID", "0"))


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
        log.debug("unhandled_event", event_name=event_type)


def _start_scheduler():
    if not SCHEDULED_REPO or not SCHEDULED_INSTALLATION_ID:
        log.info("scheduler_skipped")
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from app.handlers.schedule import (
            run_stale_check,
            run_health_report,
            run_dependency_report
        )

        scheduler = BackgroundScheduler()

        scheduler.add_job(
            run_stale_check, "cron", day_of_week="mon", hour=9,
            args=[SCHEDULED_REPO, SCHEDULED_INSTALLATION_ID], id="stale_check"
        )
        scheduler.add_job(
            run_dependency_report, "cron", day_of_week="sun", hour=10,
            args=[SCHEDULED_REPO, SCHEDULED_INSTALLATION_ID], id="dependency_report"
        )
        scheduler.add_job(
            run_health_report, "cron", day=1, hour=8,
            args=[SCHEDULED_REPO, SCHEDULED_INSTALLATION_ID], id="health_report"
        )

        scheduler.start()
        log.info("scheduler_started", repo=SCHEDULED_REPO)

    except Exception as e:
        log.error("scheduler_failed", error=str(e))


def run():
    log.info("worker_started")
    scheduler_thread = threading.Thread(target=_start_scheduler, daemon=True)
    scheduler_thread.start()

    for event_type, payload in consume_events():
        try:
            _dispatch(event_type, payload)
            metrics.increment(f"events.{event_type}.success")
        except Exception as e:
            log.error("dispatch_failed", event_name=event_type, error=str(e))
            metrics.increment(f"events.{event_type}.error")


if __name__ == "__main__":
    run()
