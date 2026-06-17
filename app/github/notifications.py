"""
Notifications - app/github/notifications.py
V4: Slack + Discord notifications with rich embeds.

FIXED (ruff E741 line 282): Renamed ambiguous `l` → `lbl` in notify_new_issue().
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
    "vulnerability_low":   False,
    "commit_lint":         False,
    "pr_reviewed":         False,
    "every_push":          False,
}

_COLORS: dict[str, int] = {
    "critical": 15158332,
    "warning":  15105570,
    "info":     3447003,
    "success":  3066993,
}

_EMOJIS: dict[str, str] = {
    "critical": "🚨",
    "warning":  "⚠️",
    "info":     "ℹ️",
    "success":  "✅",
}


def notify(
    title: str,
    message: str,
    severity: str = "info",
    repo: str = "",
    event_type: str = "",
    fields: list[dict] | None = None,
    url: str = "",
):
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
            log.warning(f"notification.slack_failed status={resp.status_code}")
    except Exception as e:
        log.error(f"notification.slack_error: {e}")


def _send_discord(
    title: str,
    message: str,
    severity: str,
    fields: list[dict],
    url: str,
):
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
                    "value":  str(f.get("value", "\u200b"))[:1024],
                    "inline": bool(f.get("inline", True)),
                }
                for f in fields[:25]
            ]

        payload = {"embeds": [embed]}
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        if resp.status_code in (200, 204):
            log.info("notification.discord_sent")
        else:
            log.warning(f"notification.discord_failed status={resp.status_code} body={resp.text[:200]}")
    except Exception as e:
        log.error(f"notification.discord_error: {e}")


def notify_secret_detected(repo: str, findings_count: int):
    notify(
        title="Secret Detected in Push",
        message=f"{findings_count} potential secret(s) found. Rotate credentials immediately.",
        severity="critical",
        repo=repo,
        event_type="secret_detected",
        fields=[
            {"name": "Findings",    "value": str(findings_count), "inline": True},
            {"name": "Repository",  "value": repo,                "inline": True},
        ],
    )


def notify_high_risk_pr(repo: str, pr_number: int, title: str):
    notify(
        title="High Risk PR Opened",
        message=f"PR #{pr_number} flagged as HIGH risk.",
        severity="warning",
        repo=repo,
        event_type="high_risk_pr",
        fields=[
            {"name": "PR",    "value": f"#{pr_number}", "inline": True},
            {"name": "Risk",  "value": "🔴 HIGH",       "inline": True},
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
            {"name": "Grade", "value": grade,         "inline": True},
            {"name": "Score", "value": f"{score}/100","inline": True},
        ],
    )


def notify_ci_failure(repo: str, branch: str, error: str):
    notify(
        title="CI Failure",
        message=error[:500],
        severity="warning",
        repo=repo,
        event_type="ci_failure",
        fields=[{"name": "Branch", "value": f"`{branch}`", "inline": True}],
    )


def notify_new_issue(repo: str, issue_number: int, title: str, labels: list):
    # FIXED (E741): Renamed `l` → `lbl`
    label_str = ", ".join(f"`{lbl}`" for lbl in labels[:5]) or "none"
    notify(
        title="New Issue Opened",
        message=f"Issue #{issue_number}: {title[:200]}",
        severity="info",
        repo=repo,
        event_type="new_issue",
        fields=[
            {"name": "Issue",  "value": f"#{issue_number}", "inline": True},
            {"name": "Labels", "value": label_str,           "inline": True},
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
            {"name": "PR",   "value": f"#{pr_number}",                      "inline": True},
            {"name": "Risk", "value": f"{risk_emoji} {risk.capitalize()}", "inline": True},
        ],
        url=f"https://github.com/{repo}/pull/{pr_number}",
    )


def notify_stale_closed(repo: str, issue_number: int, title: str, days_inactive: int):
    notify(
        title="Stale Issue Auto-Closed",
        message=f"Issue #{issue_number} closed after {days_inactive} days of inactivity.",
        severity="info",
        repo=repo,
        event_type="stale_closed",
        fields=[
            {"name": "Issue",    "value": f"#{issue_number}",      "inline": True},
            {"name": "Inactive", "value": f"{days_inactive} days", "inline": True},
            {"name": "Title",    "value": title[:200]},
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
            {"name": "CVE",     "value": cve_id,          "inline": True},
            {"name": "Fix",     "value": f"`pip install --upgrade {package}`"},
        ],
    )


def notify_all_providers_down():
    try:
        from app.ai.circuit_breaker import status_all
        statuses = status_all()
        fields = [
            {
                "name":   name,
                "value":  f"{s['state']} — recovers in {s['recovers_in_seconds']}s" if s["recovers_in_seconds"] else s["state"],
                "inline": True,
            }
            for name, s in statuses.items()
        ]
    except Exception:
        fields = []

    notify(
        title="All LLM Providers Down",
        message="No AI provider available. Tasks queued for automatic retry.",
        severity="critical",
        event_type="all_providers_down",
        fields=fields,
    )


def test_discord() -> tuple[bool, str]:
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

def send_rich_discord(
    title: str,
    description: str,
    color: int = 0x5865F2,
    fields: list = None,
    url: str = "",
):
    """
    Sprint 6: Rich Discord embed with color-coded severity.
    Colors: 0x2ECC71=green, 0xF1C40F=yellow, 0xE74C3C=red, 0x5865F2=blue
    """
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        return False, "DISCORD_WEBHOOK_URL not set"
    try:
        embed = {
            "title":       title[:256],
            "description": description[:4096],
            "color":       color,
        }
        if url:
            embed["url"] = url
        if fields:
            embed["fields"] = [
                {"name": f.get("name","")[:256],
                 "value": f.get("value","")[:1024],
                 "inline": f.get("inline", False)}
                for f in fields[:25]
            ]
        payload = {"embeds": [embed]}
        r = requests.post(webhook_url, json=payload, timeout=10)
        return r.status_code in (200, 204), f"HTTP {r.status_code}"
    except Exception as e:
        return False, str(e)


def notify_autofix_created(repo: str, issue_number: int, pr_number: int, pr_url: str):
    """Notify when bot creates an autofix PR."""
    send_rich_discord(
        title=f"🤖 Autofix PR Created — #{pr_number}",
        description=f"Auto-fix PR created for issue #{issue_number} in `{repo}`",
        color=0x2ECC71,
        fields=[
            {"name": "Repository", "value": repo, "inline": True},
            {"name": "Issue", "value": f"#{issue_number}", "inline": True},
            {"name": "PR", "value": f"[#{pr_number}]({pr_url})", "inline": True},
        ],
        url=pr_url,
    )


def notify_weekly_report(repo: str, grade: str, merged: int, closed: int):
    """Send weekly digest to Discord."""
    color_map = {"A": 0x2ECC71, "B": 0x27AE60, "C": 0xF1C40F, "D": 0xE67E22, "F": 0xE74C3C}
    send_rich_discord(
        title=f"📊 Weekly Report — {repo}",
        description=f"Grade: **{grade}**",
        color=color_map.get(grade, 0x5865F2),
        fields=[
            {"name": "PRs Merged", "value": str(merged), "inline": True},
            {"name": "Issues Closed", "value": str(closed), "inline": True},
        ],
    )
