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


def _dispatch(webhook_event: str, payload: dict):
    # NOTE: Use webhook_event= NOT event= (structlog reserves 'event' keyword)
    if webhook_event == "pull_request":
        from app.handlers.pull_request import handle
        handle(payload)
    elif webhook_event == "issues":
        from app.handlers.issues import handle
        handle(payload)
    elif webhook_event == "issue_comment":
        from app.handlers.comments import handle
        handle(payload)
    elif webhook_event == "push":
        from app.handlers.push import handle
        handle(payload)
    else:
        log.debug("unhandled_webhook_event", webhook_event=webhook_event)


def _start_scheduler():
    if not SCHEDULED_REPO or not SCHEDULED_INSTALLATION_ID:
        log.info("scheduler_skipped_no_config")
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
            args=[SCHEDULED_REPO, SCHEDULED_INSTALLATION_ID],
            id="stale_check"
        )
        scheduler.add_job(
            run_dependency_report, "cron", day_of_week="sun", hour=10,
            args=[SCHEDULED_REPO, SCHEDULED_INSTALLATION_ID],
            id="dependency_report"
        )
        scheduler.add_job(
            run_health_report, "cron", day=1, hour=8,
            args=[SCHEDULED_REPO, SCHEDULED_INSTALLATION_ID],
            id="health_report"
        )

        scheduler.start()
        log.info("scheduler_started", repo=SCHEDULED_REPO)

    except Exception as e:
        log.error("scheduler_failed", error=str(e))


def run():
    log.info("worker_started")
    scheduler_thread = threading.Thread(target=_start_scheduler, daemon=True)
    scheduler_thread.start()

    for webhook_event, payload in consume_events():
        try:
            _dispatch(webhook_event, payload)
            metrics.increment(f"events.{webhook_event}.success")
        except Exception as e:
            # NOTE: Use webhook_event= NOT event=
            log.error("dispatch_failed",
                      webhook_event=webhook_event,
                      error=str(e))
            metrics.increment(f"events.{webhook_event}.error")


if __name__ == "__main__":
    run()
