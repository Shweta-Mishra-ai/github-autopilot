"""
Tasks - app/tasks.py
V4: All Celery task definitions with retry and all-provider-fail handling.

FIXED (ruff F821): _handle_max_retries was called but never defined.
FIXED (ruff F401): Removed unused imports (get_recent, get_installation_token).
"""

import logging
import os

from celery.exceptions import MaxRetriesExceededError

from app.ai.circuit_breaker import AllProvidersDown
from app.celery_app import celery

log = logging.getLogger(__name__)


# ── Webhook event tasks ───────────────────────────────────────────────────────


@celery.task(
    bind=True,
    name="app.tasks.handle_pull_request",
    max_retries=3,
    default_retry_delay=60,
)
def handle_pull_request(self, payload: dict):
    """Process pull_request webhook events."""
    try:
        from app.handlers.pull_request import handle

        handle(payload)
    except AllProvidersDown as exc:
        _handle_all_providers_down(self, payload, exc, "PR analysis")
    except Exception as exc:
        log.error(
            f"handle_pull_request failed attempt={self.request.retries + 1}: {exc}"
        )
        try:
            raise self.retry(exc=exc, countdown=_retry_countdown(self.request.retries))
        except MaxRetriesExceededError:
            _handle_max_retries(payload, "PR processing")


@celery.task(
    bind=True,
    name="app.tasks.handle_issue_comment",
    max_retries=3,
    default_retry_delay=60,
)
def handle_issue_comment(self, payload: dict):
    """Process issue_comment webhook events (slash commands)."""
    try:
        from app.handlers.comments import handle

        handle(payload)
    except AllProvidersDown as exc:
        _handle_all_providers_down(self, payload, exc, "command")
    except Exception as exc:
        log.error(
            f"handle_issue_comment failed attempt={self.request.retries + 1}: {exc}"
        )
        try:
            raise self.retry(exc=exc, countdown=_retry_countdown(self.request.retries))
        except MaxRetriesExceededError:
            _handle_max_retries(payload, "comment command")


@celery.task(
    bind=True,
    name="app.tasks.handle_issue",
    max_retries=2,
    default_retry_delay=45,
)
def handle_issue(self, payload: dict):
    """Process issues webhook events (issue opened → triage)."""
    try:
        from app.handlers.issues import handle

        handle(payload)
    except AllProvidersDown as exc:
        _handle_all_providers_down(self, payload, exc, "issue triage")
    except Exception as exc:
        log.error(f"handle_issue failed: {exc}")
        try:
            raise self.retry(exc=exc, countdown=_retry_countdown(self.request.retries))
        except MaxRetriesExceededError:
            _handle_max_retries(payload, "issue triage")


@celery.task(
    bind=True,
    name="app.tasks.handle_push",
    max_retries=2,
    default_retry_delay=30,
)
def handle_push(self, payload: dict):
    """Process push webhook events."""
    try:
        from app.handlers.push import handle

        handle(payload)
    except AllProvidersDown as exc:
        log.warning(
            f"Push handler: all providers down, retry in {exc.retry_in_seconds}s"
        )
        raise self.retry(exc=exc, countdown=exc.retry_in_seconds)
    except Exception as exc:
        log.error(f"handle_push failed: {exc}")
        try:
            raise self.retry(exc=exc, countdown=30)
        except MaxRetriesExceededError:
            log.error("handle_push: max retries exceeded")


@celery.task(
    bind=True,
    name="app.tasks.handle_check_run",
    max_retries=2,
    default_retry_delay=30,
)
def handle_check_run(self, payload: dict):
    """Process check_run webhook events (CI pass/fail)."""
    try:
        from app.handlers.ci import handle

        handle(payload)
    except AllProvidersDown as exc:
        raise self.retry(exc=exc, countdown=exc.retry_in_seconds)
    except Exception as exc:
        log.error(f"handle_check_run failed: {exc}")
        try:
            raise self.retry(exc=exc, countdown=30)
        except MaxRetriesExceededError:
            log.error("handle_check_run: max retries exceeded")


# ── Scheduled tasks ───────────────────────────────────────────────────────────


@celery.task(
    bind=True,
    name="app.tasks.run_scheduled_tasks",
    max_retries=1,
)
def run_scheduled_tasks(self, task_name: str):
    """
    Runs a scheduled maintenance task via Celery Beat.
    task_name: "stale_check" | "health_report" | "dependency_report"

    Repos configured via MANAGED_REPOS env var:
    Format: "org/repo1:install_id1,org/repo2:install_id2"
    """
    managed = os.environ.get("MANAGED_REPOS", "")
    if not managed:
        log.info(f"scheduled.{task_name}: MANAGED_REPOS not configured — skipping")
        return

    for entry in managed.split(","):
        entry = entry.strip()
        if ":" not in entry:
            continue
        repo, install_id_str = entry.rsplit(":", 1)
        try:
            installation_id = int(install_id_str)
        except ValueError:
            log.warning(f"scheduled.{task_name}: invalid install_id in '{entry}'")
            continue

        try:
            if task_name == "stale_check":
                from app.handlers.schedule import run_stale_check

                run_stale_check(repo, installation_id)

            elif task_name == "health_report":
                from app.handlers.schedule import run_health_report

                run_health_report(repo, installation_id)

            elif task_name == "dependency_report":
                from app.handlers.schedule import run_dependency_report

                run_dependency_report(repo, installation_id)

        except Exception as e:
            log.error(f"scheduled.{task_name} repo={repo} error={e}")


@celery.task(name="app.tasks.cleanup_token_counters")
def cleanup_token_counters():
    """Log current LLM token usage. Redis keys auto-expire at 24h."""
    try:
        import datetime
        from app.core.redis_client import get_redis

        r = get_redis()
        today = datetime.date.today().isoformat()

        for provider in ("groq_70b", "groq_8b", "gemini", "openrouter"):
            tokens = r.get(f"llm:tokens:{provider}:{today}") or 0
            reqs = r.get(f"llm:requests:{provider}:{today}") or 0
            log.info(
                f"token_counter provider={provider} tokens={tokens} requests={reqs}"
            )
    except Exception as e:
        log.warning(f"cleanup_token_counters: {e}")


# ── Private helpers ───────────────────────────────────────────────────────────


def _retry_countdown(retries: int) -> int:
    """Exponential backoff: 60s → 120s → 240s."""
    return 60 * (2**retries)


def _handle_max_retries(payload: dict, task_desc: str):
    """
    Called when all Celery retries are exhausted (non-provider-down failures).
    Posts a failure notice to GitHub and logs the incident.
    FIXED (ruff F821): Was called in 3 tasks but never defined.
    """
    log.error(f"tasks.max_retries_exceeded task={task_desc}")
    _post_failure_message(payload, task_desc)


def _handle_all_providers_down(
    task,
    payload: dict,
    exc: AllProvidersDown,
    task_desc: str,
):
    """
    When all LLM providers are OPEN (rate-limited / down):
    1. First attempt → post "queued" message so user knows it's coming.
    2. Retry with countdown until providers recover.
    3. All retries exhausted → post failure message.
    """
    if task.request.retries == 0:
        _post_queued_message(payload, exc.retry_in_seconds, task_desc)

    try:
        raise task.retry(exc=exc, countdown=exc.retry_in_seconds + 10)
    except MaxRetriesExceededError:
        _post_failure_message(payload, task_desc)
        _notify_providers_down()


def _post_queued_message(payload: dict, retry_in: int, task_desc: str):
    """Post '⏳ AI Queued' comment so user doesn't need to retype."""
    try:
        from app.github.auth import get_installation_token
        from app.github.client import gh_post

        installation_id = payload.get("installation", {}).get("id")
        repo = payload.get("repository", {}).get("full_name", "")
        issue_number = payload.get("issue", {}).get("number") or payload.get(
            "pull_request", {}
        ).get("number")
        if not (installation_id and repo and issue_number):
            return

        token = get_installation_token(installation_id)
        wait_min = max(1, retry_in // 60)

        gh_post(
            f"/repos/{repo}/issues/{issue_number}/comments",
            token,
            {
                "body": (
                    f"## ⏳ AI Request Queued\n\n"
                    f"All AI providers are temporarily busy (rate limited).\n\n"
                    f"Your **{task_desc}** request has been queued and will be "
                    f"processed automatically in **~{wait_min} minute(s)**.\n\n"
                    "No need to type the command again — I'll post the result here.\n\n"
                    "---\n*🤖 AI Repo Manager V4*"
                )
            },
        )
    except Exception as e:
        log.warning(f"_post_queued_message failed: {e}")


def _post_failure_message(payload: dict, task_desc: str):
    """Post permanent failure notice after all retries exhausted."""
    try:
        from app.ai.circuit_breaker import status_all
        from app.github.auth import get_installation_token
        from app.github.client import gh_post

        installation_id = payload.get("installation", {}).get("id")
        repo = payload.get("repository", {}).get("full_name", "")
        issue_number = payload.get("issue", {}).get("number") or payload.get(
            "pull_request", {}
        ).get("number")
        if not (installation_id and repo and issue_number):
            return

        token = get_installation_token(installation_id)
        statuses = status_all()
        status_lines = "\n".join(
            f"- **{name}**: {s['state']}"
            + (
                f" (recovers in {s['recovers_in_seconds']}s)"
                if s["recovers_in_seconds"]
                else ""
            )
            for name, s in statuses.items()
        )

        gh_post(
            f"/repos/{repo}/issues/{issue_number}/comments",
            token,
            {
                "body": (
                    f"## ⚠️ AI Temporarily Unavailable\n\n"
                    f"Your **{task_desc}** request could not be completed "
                    f"after multiple retries.\n\n"
                    f"**Provider Status:**\n{status_lines}\n\n"
                    "Please try again in **30 minutes**.\n\n"
                    "---\n*🤖 AI Repo Manager V4*"
                )
            },
        )
    except Exception as e:
        log.warning(f"_post_failure_message failed: {e}")


def _notify_providers_down():
    """Alert Discord/Slack that all providers are down."""
    try:
        from app.github.notifications import notify_all_providers_down

        notify_all_providers_down()
    except Exception:
        pass
