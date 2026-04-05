"""
Notifications - app/github/notifications.py
V4 changes:

FIXED (LOOPHOLE 17): Hardcoded 37 days removed.
  notify_stale_closed() now accepts days_inactive param.
  schedule.py must pass the actual days when calling.

FIXED (Discord): Rich embed fields now correct.
  Added Content-Type header (was missing — Discord silently rejected).
  Added timestamp in ISO format (Discord renders as local time per user).
  Added url field on embed title for direct links.
  All colors match Discord's decimal color spec.

NEW: notify_all_providers_down() — alerts when all LLM circuits OPEN.
NEW: test_discord() — returns (bool, str) for /test-discord endpoint.
NEW: Rich fields on every notification type.
"""

import logging
import os
import threading
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

SLACK_WEBHOOK_URL   = os.environ.get("SLACK_WEBHOOK_URL", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
SLACK_ENABLED       = bool(SLACK_WEBHOOK_URL)
DISCORD_ENABLED     = bool(DISCORD_WEBHOOK_URL)

# Event filter — True = send, False = suppress (too noisy)
NOTIFY_FILTER: dict[str, bool] = {
    "secret_detected":     True,
    "vulnerability_high":  True,
    "auto_merge":          True,
    "high_risk_pr":        True,
    "pr_opened":           True,
    "new_issue":           True,
    "health_degraded":     True,
    "ci_failure":          True,
    "stale_closed":        True,
    "all_providers_down":  True,
    # Suppressed — too noisy
    "vulnerability_low":   False,
    "commit_lint":         False,
    "pr_reviewed":         False,
    "every_push":          False,
}

# Discord embed colors (decimal integers — required by Discord API)
_COLORS: dict[str, int] = {
    "critical": 15158332,   # Red     #E74C3C
    "warning":  15105570,   # Orange  #E67E22
    "info":     3447003,    # Blue    #3498DB
    "success":  3066993,    # Green   #2ECC71
}

_EMOJIS: dict[str, str] = {
    "critical": "🚨",
    "warning":  "⚠️",
    "info":     "ℹ️",
    "success":  "✅",
}


# ── Core send function ────────────────────────────────────────────────────────

def notify(
    title: str,
    message: str,
    severity: str = "info",
    repo: str = "",
    event_type: str = "",
    fields: list[dict] | None = None,
    url: str = "",
):
    """
    Send notification to Slack and/or Discord in background threads.
    Never blocks the webhook processing path.

    Args:
        title:      Short title for the notification
        message:    Main body text
        severity:   "info" | "warning" | "critical" | "success"
        repo:       Repository full name e.g. "org/repo"
        event_type: Filter key from NOTIFY_FILTER
        fields:     Discord embed fields [{"name": "...", "value": "...", "inline": True}]
        url:        Clickable URL on the embed title (Discord only)
    """
    if event_type and not NOTIFY_FILTER.get(event_type, True):
        log.debug(f"notification.suppressed event_type={event_type}")
        return

    if not SLACK_ENABLED and not DISCORD_ENABLED:
        log.debug("notification.skipped no_webhooks_configured")
        return

    emoji      = _EMOJIS.get(severity, "ℹ️")
    full_title = f"{emoji} {title}"
    if repo:
        full_title += f" — `{repo}`"

    threads: list[threading.Thread] = []

    if SLACK_ENABLED:
        t = threading.Thread(
            target=_send_slack,
            args=(full_title, message, severity),
            daemon=True,
        )
        threads.append(t)

    if DISCORD_ENABLED:
        t = threading.Thread(
            target=_send_discord,
            args=(full_title, message, severity, fields or [], url),
            daemon=True,
        )
        threads.append(t)

    for t in threads:
        t.start()


def _send_slack(title: str, message: str, severity: str):
    """Send Slack attachment-style notification."""
    color_map = {
        "critical": "#E74C3C",
        "warning":  "#E67E22",
        "info":     "#3498DB",
        "success":  "#2ECC71",
    }
    try:
        payload = {
            "attachments": [{
                "color":  color_map.get(severity, "#3498DB"),
                "title":  title,
                "text":   message[:1000],
                "footer": "AI Repo Manager V4",
                "ts":     int(datetime.now(timezone.utc).timestamp()),
            }]
        }
        resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=5)
        if resp.status_code == 200:
            log.info("notification.slack_sent")
        else:
            log.warning(
                f"notification.slack_failed "
                f"status={resp.status_code} body={resp.text[:100]}"
            )
    except Exception as e:
        log.error(f"notification.slack_error: {e}")


def _send_discord(
    title: str,
    message: str,
    severity: str,
    fields: list[dict],
    url: str,
):
    """
    Send Discord rich embed notification.

    FIXED: Added Content-Type header (was silently rejected without it).
    FIXED: Timestamp in ISO format (Discord auto-converts to user's timezone).
    FIXED: url field makes title clickable (links to PR/issue directly).
    FIXED: Fields properly capped at Discord limits (25 fields, 256/1024 chars).
    """
    try:
        color = _COLORS.get(severity, _COLORS["info"])

        embed: dict = {
            "title":       title[:256],
            "description": message[:4096],
            "color":       color,
            "timestamp":   datetime.now(timezone.utc).isoformat(),
            "footer":      {"text": "AI Repo Manager V4"},
        }

        if url:
            embed["url"] = url

        if fields:
            embed["fields"] = [
                {
                    "name":   str(f.get("name", ""))[:256],
                    "value":  str(f.get("value", "\u200b"))[:1024],  # \u200b = zero-width space (Discord requires non-empty)
                    "inline": bool(f.get("inline", True)),
                }
                for f in fields[:25]
            ]

        payload = {"embeds": [embed]}

        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},  # ← Critical, was missing
            timeout=5,
        )

        if resp.status_code in (200, 204):
            log.info("notification.discord_sent")
        else:
            log.warning(
                f"notification.discord_failed "
                f"status={resp.status_code} body={resp.text[:200]}"
            )

    except Exception as e:
        log.error(f"notification.discord_error: {e}")


# ── Typed helpers ─────────────────────────────────────────────────────────────

def notify_secret_detected(repo: str, findings_count: int):
    notify(
        title="Secret Detected in Push",
        message=(
            f"{findings_count} potential secret(s) found.\n"
            "Rotate any exposed credentials immediately."
        ),
        severity="critical",
        repo=repo,
        event_type="secret_detected",
        fields=[
            {"name": "Findings", "value": str(findings_count), "inline": True},
            {"name": "Repository", "value": repo, "inline": True},
            {"name": "Action", "value": "Rotate credentials + add to .gitignore"},
        ],
    )


def notify_high_risk_pr(repo: str, pr_number: int, title: str):
    notify(
        title="High Risk PR Opened",
        message=f"PR #{pr_number} has been flagged as HIGH risk.",
        severity="warning",
        repo=repo,
        event_type="high_risk_pr",
        fields=[
            {"name": "PR", "value": f"#{pr_number}", "inline": True},
            {"name": "Risk", "value": "🔴 HIGH", "inline": True},
            {"name": "Title", "value": title[:200]},
        ],
        url=f"https://github.com/{repo}/pull/{pr_number}",
    )


def notify_health_degraded(repo: str, grade: str, score: int):
    notify(
        title="Repo Health Degraded",
        message=f"Repository health is now **{grade}** ({score}/100).",
        severity="warning",
        repo=repo,
        event_type="health_degraded",
        fields=[
            {"name": "Grade", "value": grade, "inline": True},
            {"name": "Score", "value": f"{score}/100", "inline": True},
        ],
    )


def notify_ci_failure(repo: str, branch: str, error: str):
    notify(
        title="CI Failure",
        message=error[:500],
        severity="warning",
        repo=repo,
        event_type="ci_failure",
        fields=[
            {"name": "Branch", "value": f"`{branch}`", "inline": True},
        ],
    )


def notify_new_issue(repo: str, issue_number: int, title: str, labels: list):
    label_str = ", ".join(f"`{l}`" for l in labels[:5]) or "none"
    notify(
        title="New Issue Opened",
        message=f"Issue #{issue_number}: {title[:200]}",
        severity="info",
        repo=repo,
        event_type="new_issue",
        fields=[
            {"name": "Issue", "value": f"#{issue_number}", "inline": True},
            {"name": "Labels", "value": label_str, "inline": True},
        ],
        url=f"https://github.com/{repo}/issues/{issue_number}",
    )


def notify_pr_opened(repo: str, pr_number: int, title: str, risk: str = "unknown"):
    risk_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴", "unknown": "⏳"}.get(risk, "⏳")
    notify(
        title="New PR Opened",
        message=f"PR #{pr_number}: {title[:200]}",
        severity="warning" if risk == "high" else "info",
        repo=repo,
        event_type="pr_opened",
        fields=[
            {"name": "PR", "value": f"#{pr_number}", "inline": True},
            {"name": "Risk", "value": f"{risk_emoji} {risk.capitalize()}", "inline": True},
        ],
        url=f"https://github.com/{repo}/pull/{pr_number}",
    )


def notify_stale_closed(repo: str, issue_number: int, title: str, days_inactive: int):
    """
    ✅ FIXED (LOOPHOLE 17): Was hardcoded 37 days.
    Now accepts actual days_inactive param from schedule.py.
    """
    notify(
        title="Stale Issue Auto-Closed",
        message=f"Issue #{issue_number} closed after {days_inactive} days of inactivity.",
        severity="info",
        repo=repo,
        event_type="stale_closed",
        fields=[
            {"name": "Issue", "value": f"#{issue_number}", "inline": True},
            {"name": "Inactive", "value": f"{days_inactive} days", "inline": True},
            {"name": "Title", "value": title[:200]},
        ],
        url=f"https://github.com/{repo}/issues/{issue_number}",
    )


def notify_vulnerability(repo: str, package: str, severity: str, cve_id: str):
    level = severity.lower()
    notify(
        title=f"Vulnerability — {severity.upper()}",
        message=f"Package `{package}` has a known vulnerability.",
        severity="critical" if level == "high" else "warning",
        repo=repo,
        event_type=f"vulnerability_{level}",
        fields=[
            {"name": "Package", "value": f"`{package}`", "inline": True},
            {"name": "CVE/GHSA", "value": cve_id, "inline": True},
            {"name": "Fix", "value": f"`pip install --upgrade {package}`"},
        ],
    )


def notify_all_providers_down():
    """NEW V4: Alert when all LLM circuit breakers are OPEN."""
    try:
        from app.ai.circuit_breaker import status_all
        statuses = status_all()
        fields = [
            {
                "name":   name,
                "value":  (
                    f"{s['state']} — "
                    f"recovers in {s['recovers_in_seconds']}s"
                    if s["recovers_in_seconds"] else s["state"]
                ),
                "inline": True,
            }
            for name, s in statuses.items()
        ]
    except Exception:
        fields = []

    notify(
        title="All LLM Providers Down",
        message="No AI provider available. Tasks are queued for automatic retry.",
        severity="critical",
        event_type="all_providers_down",
        fields=fields,
    )


def test_discord() -> tuple[bool, str]:
    """
    NEW V4: Test Discord webhook manually.
    Called by /test-discord endpoint.
    Returns (success: bool, message: str).
    """
    if not DISCORD_ENABLED:
        return False, "DISCORD_WEBHOOK_URL environment variable is not set"

    try:
        payload = {
            "embeds": [{
                "title":       "✅ AI Repo Manager V4 — Discord Test",
                "description": "Discord webhook is connected and working correctly!",
                "color":       _COLORS["success"],
                "timestamp":   datetime.now(timezone.utc).isoformat(),
                "footer":      {"text": "AI Repo Manager V4"},
                "fields": [
                    {"name": "Status",  "value": "Connected", "inline": True},
                    {"name": "Version", "value": "V4.0",      "inline": True},
                ],
            }]
        }
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code in (200, 204):
            return True, "Discord notification sent successfully ✅"
        return False, f"Discord returned HTTP {resp.status_code}: {resp.text[:150]}"

    except Exception as e:
        return False, f"Exception: {e}"
