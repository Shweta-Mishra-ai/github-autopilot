"""
Celery App - app/celery_app.py
V4: Celery instance with Beat schedule.
Single entry point for all async task processing.

Free tier setup: Worker + Beat run in SAME process (--beat flag).
Paid tier: Split into separate services in render.yaml.
"""

import os
from celery import Celery
from celery.schedules import crontab

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

celery = Celery(
    "github_autopilot_v4",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["app.tasks"],
)

celery.conf.update(
    # Serialization
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    result_expires=3600,  # Results kept 1 hour
    timezone="UTC",
    enable_utc=True,
    # Reliability
    task_acks_late=True,  # Ack AFTER task completes (not on receive)
    task_reject_on_worker_lost=True,  # Re-queue if worker crashes mid-task
    worker_prefetch_multiplier=1,  # One task at a time per worker (prevents starvation)
    # Retry defaults (per-task can override)
    task_annotations={
        "*": {
            "max_retries": 3,
            "default_retry_delay": 60,  # 60s base delay
        }
    },
    # Task routing — 3 priority queues
    # high:   PR events, comment commands (user is waiting)
    # medium: Push events, issue triage
    # low:    Scheduled reports (no one waiting)
    task_routes={
        "app.tasks.handle_pull_request": {"queue": "high"},
        "app.tasks.handle_issue_comment": {"queue": "high"},
        "app.tasks.handle_issue": {"queue": "medium"},
        "app.tasks.handle_push": {"queue": "medium"},
        "app.tasks.handle_check_run": {"queue": "medium"},
        "app.tasks.run_scheduled_tasks": {"queue": "low"},
    },
    # Default queue if route not matched
    task_default_queue="medium",
    # Beat schedule (replaces APScheduler)
    beat_schedule={
        # Stale issue check — daily at 9 AM UTC
        "stale-check-daily": {
            "task": "app.tasks.run_scheduled_tasks",
            "schedule": crontab(hour=9, minute=0),
            "args": ("stale_check",),
            "options": {"queue": "low"},
        },
        # Monthly health report — 1st of each month at 10 AM UTC
        "health-report-monthly": {
            "task": "app.tasks.run_scheduled_tasks",
            "schedule": crontab(day_of_month=1, hour=10, minute=0),
            "args": ("health_report",),
            "options": {"queue": "low"},
        },
        # Weekly dependency report — Monday 8 AM UTC
        "dependency-report-weekly": {
            "task": "app.tasks.run_scheduled_tasks",
            "schedule": crontab(day_of_week=1, hour=8, minute=0),
            "args": ("dependency_report",),
            "options": {"queue": "low"},
        },
        # LLM token budget reset check — every hour
        # Cleans up expired Redis usage counters
        "token-budget-cleanup-hourly": {
            "task": "app.tasks.cleanup_token_counters",
            "schedule": crontab(minute=0),  # Every hour at :00
            "options": {"queue": "low"},
        },
    },
    # Logging
    worker_log_format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    worker_task_log_format="%(asctime)s [%(levelname)s] %(task_name)s[%(task_id)s]: %(message)s",
)
