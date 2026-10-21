"""
app/github/redelivery.py
Ask GitHub to redeliver the webhooks this App failed to accept.

GitHub does NOT redeliver a failed webhook on its own. A delivery fails when
the server does not answer within 10 seconds or answers with an error, and
that event is then gone unless someone presses "Redeliver" in the App's
settings. Several places in this codebase assumed the opposite ("503 so
GitHub retries") — and on Render's free tier the cases are routine:

  - the service sleeps when idle and a cold start takes far longer than 10s,
    so the webhook that wakes it is recorded as failed;
  - a full queue or a saturated pool answers 503;
  - the per-IP limit answers 429.

GitHub's documented remedy is a scheduled job that lists recent deliveries
and redelivers the failed ones
(https://docs.github.com/webhooks/using-webhooks/handling-failed-webhook-deliveries).
This is that job, run in-process: once shortly after boot — which is exactly
when a cold start has just lost a delivery — and every few minutes after.

Only failures the server did not mean are retried: no response, 429 and 5xx.
A 400/401/413 is a deliberate refusal (bad signature, unknown event, too
large) and redelivering it would fail the same way. Each delivery is retried
at most MAX_ATTEMPTS times. A redelivered event that had in fact been
processed (a slow cold start can answer after GitHub stopped waiting) is
dropped by the payload-based idempotency check.

Uses the App's JWT, so it needs GITHUB_APP_ID and GITHUB_PRIVATE_KEY and no
installation token. WEBHOOK_REDELIVERY=0 turns it off.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time

import requests

log = logging.getLogger(__name__)

API = "https://api.github.com"
FIRST_SWEEP_SECONDS = 45  # long enough for the server to be accepting webhooks
SWEEP_SECONDS = 600
WINDOW_SECONDS = 6 * 3600  # how far back to look; GitHub allows 7 days
MAX_PAGES = 5
MAX_ATTEMPTS = 2  # redelivery requests per delivery guid
_LOCK_KEY = "redelivery:lock"
_LAST_KEY = "redelivery:last_result"

_thread: threading.Thread | None = None


def enabled() -> bool:
    if os.environ.get("WEBHOOK_REDELIVERY", "").strip().lower() in ("0", "false", "no"):
        return False
    return bool(os.environ.get("GITHUB_APP_ID") and os.environ.get("GITHUB_PRIVATE_KEY"))


def retryable(status_code) -> bool:
    """A failure the server did not mean: no response, rate limited, 5xx."""
    try:
        code = int(status_code or 0)
    except (TypeError, ValueError):
        return True
    return code == 0 or code == 429 or code >= 500


def _headers() -> dict:
    from app.github.auth import get_jwt

    return {
        "Authorization": f"Bearer {get_jwt()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _parse_time(value: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def _next_cursor(link_header: str) -> str:
    m = re.search(r'<[^>]*[?&]cursor=([^&>]+)[^>]*>;\s*rel="next"', link_header or "")
    return m.group(1) if m else ""


def recent_deliveries(now: float, get=requests.get) -> list[dict]:
    """Deliveries within WINDOW_SECONDS, newest first. Raises on an API error."""
    out: list[dict] = []
    cursor = ""
    headers = _headers()
    for _ in range(MAX_PAGES):
        params = {"per_page": 100}
        if cursor:
            params["cursor"] = cursor
        r = get(f"{API}/app/hook/deliveries", headers=headers, params=params, timeout=15)
        r.raise_for_status()
        page = r.json() or []
        for d in page:
            try:
                if now - _parse_time(d.get("delivered_at", "")) > WINDOW_SECONDS:
                    return out
            except (TypeError, ValueError):
                continue
            out.append(d)
        cursor = _next_cursor(r.headers.get("Link", ""))
        if not page or not cursor:
            break
    else:
        # Every page read and the window not reached: older failures in it
        # are not retried this sweep. Said, not left to look like the end.
        log.warning(
            f"redelivery.page_limit read={len(out)} deliveries in {MAX_PAGES} pages without "
            f"reaching the {WINDOW_SECONDS // 3600}h window — older failures not checked"
        )
    return out


def failed_guids(deliveries: list[dict]) -> dict[str, dict]:
    """
    guid -> the latest attempt, for every delivery none of whose attempts
    succeeded and whose latest failure is retryable. Every attempt of one
    event (the original and each redelivery) shares its guid.
    """
    by_guid: dict[str, list[dict]] = {}
    for d in deliveries:
        if d.get("guid"):
            by_guid.setdefault(d["guid"], []).append(d)
    out = {}
    for guid, attempts in by_guid.items():
        if any(200 <= int(a.get("status_code") or 0) < 300 for a in attempts):
            continue
        latest = max(attempts, key=lambda a: str(a.get("delivered_at", "")))
        if retryable(latest.get("status_code")):
            out[guid] = latest
    return out


def sweep(now: float | None = None, get=requests.get, post=requests.post) -> dict:
    """
    One pass: redeliver every recent retryable failure, at most MAX_ATTEMPTS
    times per event. Returns counts. Raises when GitHub cannot be read.
    """
    from app.core.redis_client import get_redis

    now = now or time.time()
    r = get_redis()
    failed = failed_guids(recent_deliveries(now, get=get))
    requested, gave_up, errors = 0, 0, 0
    for guid, latest in failed.items():
        key = f"redelivery:attempts:{guid}"
        tries = int(r.get(key) or 0)
        if tries >= MAX_ATTEMPTS:
            gave_up += 1
            continue
        resp = post(
            f"{API}/app/hook/deliveries/{latest['id']}/attempts", headers=_headers(), timeout=15
        )
        if resp.status_code in (200, 201, 202):
            r.set(key, str(tries + 1), ex=8 * 86400)
            requested += 1
            log.warning(
                f"redelivery.requested event={latest.get('event')} action={latest.get('action')} "
                f"guid={guid} last_status={latest.get('status_code')} attempt={tries + 1}"
            )
        else:
            errors += 1
            log.error(f"redelivery.request_failed guid={guid} status={resp.status_code}")
    if gave_up:
        log.error(
            f"redelivery.gave_up n={gave_up} — these events failed {MAX_ATTEMPTS} redeliveries; "
            "redeliver them from the App's Advanced settings once the cause is fixed"
        )
    result = {
        "at": int(now),
        "failed": len(failed),
        "requested": requested,
        "gave_up": gave_up,
        "errors": errors,
    }
    try:
        import json

        r.set(_LAST_KEY, json.dumps(result), ex=86400)
    except Exception as e:  # nosec B110 - the status line is informational
        log.debug(f"redelivery.status_not_stored: {e}")
    return result


def tick() -> dict | None:
    """One sweep, if this process wins the lock (several processes share Redis)."""
    if not enabled():
        return None
    from app.core.redis_client import get_redis

    if not get_redis().set(_LOCK_KEY, "1", nx=True, ex=SWEEP_SECONDS // 2):
        return None
    return sweep()


def last_result() -> dict | None:
    try:
        import json

        from app.core.redis_client import get_redis

        raw = get_redis().get(_LAST_KEY)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def start_scheduler() -> bool:
    """Start the sweep thread. Idempotent; True when running."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return True
    if not enabled():
        log.info(
            "redelivery.disabled (needs GITHUB_APP_ID and GITHUB_PRIVATE_KEY; WEBHOOK_REDELIVERY=0 turns it off)"
        )
        return False

    def _loop() -> None:
        delay = FIRST_SWEEP_SECONDS
        while True:
            time.sleep(delay)
            delay = SWEEP_SECONDS
            try:
                tick()
            except Exception as e:  # a failed sweep must not end the schedule
                log.error(f"redelivery.sweep_failed: {type(e).__name__}: {e}")

    _thread = threading.Thread(target=_loop, daemon=True, name="redelivery")
    _thread.start()
    log.info(f"redelivery.started every={SWEEP_SECONDS}s window={WINDOW_SECONDS}s")
    return True
