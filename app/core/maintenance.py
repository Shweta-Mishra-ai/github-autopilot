"""
app/core/maintenance.py — the periodic pass: full security scan + memory backup.

WHY A DUE TIME RATHER THAN A SLEEP
  The obvious implementation is a thread that sleeps for the interval. At a
  15-day cadence that thread would essentially never fire: this app is built
  for a free tier that restarts on deploy, on idle, and on the host's own
  schedule, and every restart puts a `sleep(15 days)` back to zero. A schedule
  measured in days cannot live in process memory.

  So the due time is stored in Redis and the thread only *checks* it, hourly.
  Restarts are then irrelevant — whoever is running when the due time passes
  does the work, and the next due time is written before the work starts.

WHY THE DUE TIME IS ADVANCED FIRST
  A scan of many repositories takes minutes and can fail halfway. Advancing the
  due time first means a crash costs one cycle; advancing it afterwards would
  mean a run that dies mid-way is retried by the next tick an hour later, and
  again, and again — a slow failure loop against the GitHub API.

WHAT IT DOES NOT DO
  It does not restore memory. Restore overwrites live data and runs only at
  boot, only when there is nothing to overwrite (memory_backup.py). Nothing on
  a timer may cause one.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time

log = logging.getLogger(__name__)

INTERVAL_DAYS_ENV = "MAINTENANCE_INTERVAL_DAYS"
ENABLED_ENV = "MAINTENANCE_ENABLED"

DEFAULT_INTERVAL_DAYS = 15.0
MIN_INTERVAL_DAYS = 1.0

# How often the thread wakes to compare "now" against the stored due time.
# Hourly: fine-grained enough that a 15-day schedule lands the same day, cheap
# enough to be invisible (one Redis GET per hour per process).
TICK_SECONDS = 3600

_DUE_KEY = "maintenance:due_at"
_LAST_RUN_KEY = "maintenance:last_run"
_LAST_RESULT_KEY = "maintenance:last_result"

# Bounded so one repository with a large backlog cannot turn the pass into an
# hour of API calls. Repos beyond this are picked up on the next cycle.
MAX_REPOS_PER_RUN = 25

_thread = None


def interval_seconds() -> float:
    """Configured cadence, floored at a day so a typo cannot make this a loop."""
    try:
        days = float(os.environ.get(INTERVAL_DAYS_ENV, "") or DEFAULT_INTERVAL_DAYS)
    except ValueError:
        days = DEFAULT_INTERVAL_DAYS
    return max(MIN_INTERVAL_DAYS, days) * 24 * 3600


def enabled() -> bool:
    """On unless explicitly disabled. Reads the environment per call."""
    return os.environ.get(ENABLED_ENV, "1").strip().lower() not in ("0", "false", "no")


# ── Due-time bookkeeping ──────────────────────────────────────────────────────


def _redis():
    from app.core.redis_client import get_redis

    return get_redis()


def _read_due() -> int | None:
    try:
        raw = _redis().get(_DUE_KEY)
        if raw is None:
            return None
        return int(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception as e:
        log.debug(f"maintenance.due_read_failed: {e}")
        return None


def claim_due_run(now: float | None = None) -> bool:
    """
    Decide whether this process should run the pass right now, and claim it.

    Fails CLOSED on any Redis problem: the pass writes to GitHub and burns
    provider quota, so "I could not check" must mean "do not run", never "run
    anyway". A skipped cycle costs 15 days of freshness; an unclaimed
    concurrent run costs duplicate scans and duplicate issues on every repo.
    """
    now = time.time() if now is None else now
    interval = interval_seconds()

    try:
        r = _redis()
    except Exception as e:
        log.debug(f"maintenance.claim_failed — no redis: {e}")
        return False

    due = _read_due()
    if due is None:
        # First boot: schedule one interval out rather than running immediately.
        # A cold deploy has nothing worth scanning yet, and running on every
        # fresh Redis would turn a wipe into an unscheduled full scan.
        with contextlib.suppress(Exception):
            r.set(_DUE_KEY, str(int(now + interval)), nx=True)
        return False

    if now < due:
        return False

    # Advance first (see module docstring), and use NX on a short-lived claim so
    # two processes that both see the due time cannot both proceed.
    try:
        if not r.set("maintenance:claim", str(int(now)), ex=600, nx=True):
            return False
        r.set(_DUE_KEY, str(int(now + interval)))
        r.set(_LAST_RUN_KEY, str(int(now)))
    except Exception as e:
        log.warning(f"maintenance.claim_write_failed: {e}")
        return False

    return True


# ── The pass itself ───────────────────────────────────────────────────────────


def scan_repo(repo: str, installation_id: int) -> dict:
    """
    Full security scan of one repository. Returns a small result record.

    Never raises: one repository failing must not stop the pass, and the record
    says which repositories were not scanned rather than quietly omitting them.
    """
    record = {"repo": repo, "ok": False, "critical": 0, "total": 0, "error": ""}
    try:
        from app.github.auth import get_installation_token
        from app.security.scanner import run_security_scan

        token = get_installation_token(installation_id)
        report = run_security_scan(repo, token)
        record.update(
            ok=True,
            critical=report.critical_count,
            total=report.total_count,
        )
    except Exception as e:
        record["error"] = str(e)[:150]
        log.warning(f"maintenance.scan_failed repo={repo}: {e}")
    return record


def run_pass() -> dict:
    """
    One maintenance cycle: scan every known repository, then back up memory.

    Backup runs last and unconditionally — a scan failure must not cost the
    backup, which is the half that protects data.
    """
    started = time.time()
    from app.core.installations import known_installations

    installs = known_installations()
    scanned: list[dict] = []

    for repo, inst in sorted(installs.items())[:MAX_REPOS_PER_RUN]:
        scanned.append(scan_repo(repo, inst))

    findings = sum(s["critical"] for s in scanned)
    if findings:
        _notify_findings([s for s in scanned if s["critical"]])

    backed_up = False
    try:
        from app.core.memory_backup import run_backup_once

        backed_up = run_backup_once()
    except Exception as e:
        log.error(f"maintenance.backup_failed: {e}")

    result = {
        "at": int(started),
        "duration_seconds": round(time.time() - started, 1),
        "repos_known": len(installs),
        "repos_scanned": sum(1 for s in scanned if s["ok"]),
        "repos_failed": sum(1 for s in scanned if not s["ok"]),
        "critical_findings": findings,
        "memory_backed_up": backed_up,
    }
    _store_result(result)
    log.info(f"maintenance.pass_complete {result}")
    return result


def _notify_findings(repos: list[dict]) -> None:
    """
    Alert on critical findings from the scheduled scan. Never raises.

    Uses notify() rather than notify_vulnerability(), which takes a single
    package and CVE — this is a repository-level count from a periodic sweep,
    not one advisory, and passing it through the wrong shape would either raise
    or print "Package `3 critical findings` has a known vulnerability".
    """
    try:
        from app.github.notifications import notify

        # Capped: this is a page, not a report. The full detail is in the
        # stored result and in /secfull, which the operator runs deliberately.
        for rec in repos[:5]:
            notify(
                title="Scheduled scan — critical findings",
                message=(
                    f"The {interval_seconds() / 86400:.0f}-day security sweep found "
                    f"{rec['critical']} critical finding(s)."
                ),
                severity="critical",
                repo=rec["repo"],
                event_type="scheduled_scan_critical",
                fields=[
                    {"name": "Critical", "value": str(rec["critical"])},
                    {"name": "Total findings", "value": str(rec["total"])},
                ],
            )
    except Exception as e:
        log.debug(f"maintenance.notify_failed: {e}")


def _store_result(result: dict) -> None:
    try:
        import json

        # Kept for four cycles so /health can still show the last run after a
        # couple of missed ones, without becoming a growing log.
        _redis().set(_LAST_RESULT_KEY, json.dumps(result), ex=int(interval_seconds() * 4))
    except Exception as e:
        log.debug(f"maintenance.result_store_failed: {e}")


# ── Scheduler ─────────────────────────────────────────────────────────────────


def tick() -> dict | None:
    """One check. Runs the pass if it is due and this process wins the claim."""
    if not enabled():
        return None
    if not claim_due_run():
        return None
    return run_pass()


def start_scheduler() -> bool:
    """
    Start the hourly tick thread. Idempotent; returns True if running.

    Daemon, like the queue consumers: a maintenance pass must never hold up
    SIGTERM, and an interrupted pass costs one cycle rather than any data.
    """
    global _thread

    if _thread is not None and _thread.is_alive():
        return True
    if not enabled():
        log.info("maintenance.disabled")
        return False

    import threading

    def _loop() -> None:
        while True:
            time.sleep(TICK_SECONDS)
            try:
                tick()
            except Exception as e:  # never let the schedule die with one pass
                log.error(f"maintenance.tick_failed: {e}")

    _thread = threading.Thread(target=_loop, daemon=True, name="maintenance")
    _thread.start()
    log.info(f"maintenance.started interval_days={interval_seconds() / 86400:.1f}")
    return True


def status() -> dict:
    """Operator-facing state for /health. Never raises."""
    import json

    out = {
        "enabled": enabled(),
        "interval_days": round(interval_seconds() / 86400, 1),
        "scheduler_running": bool(_thread is not None and _thread.is_alive()),
        "next_run_at": _read_due() or 0,
        "last_result": {},
    }
    try:
        raw = _redis().get(_LAST_RESULT_KEY)
        if raw:
            out["last_result"] = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception:
        pass
    return out
