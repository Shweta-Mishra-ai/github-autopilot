"""
Notifications - app/github/notifications.py
V3: Slack and Discord webhook notifications.
Configure via environment variables:
  SLACK_WEBHOOK_URL - Slack incoming webhook URL
  DISCORD_WEBHOOK_URL - Discord webhook URL
"""
import os
import threading
import requests
import logging

log = logging.getLogger(__name__)

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
SLACK_ENABLED = bool(SLACK_WEBHOOK_URL)
DISCORD_ENABLED = bool(DISCORD_WEBHOOK_URL)

# ✅ UPDATED — new_issue, pr_opened, stale_closed add kiye
# True = bhejo, False = skip (too noisy)
NOTIFY_FILTER = {
    # 🚨 CRITICAL — hamesha bhejo
    "secret_detected": True,
    "vulnerability_high": True,
    "auto_merge": True,

    # ⚠️ WARNING — important events
    "high_risk_pr": True,
    "pr_opened": True,          # ✅ NEW
    "new_issue": True,          # ✅ NEW
    "health_degraded": True,
    "ci_failure": True,
    "stale_closed": True,       # ✅ NEW

    # ❌ SKIP — too noisy
    "vulnerability_low": False,
    "commit_lint": False,
    "pr_reviewed": False,
    "every_push": False,
    "health_on_every_push": False,
}


def notify(title: str, message: str, severity: str = "info", repo: str = "", event_type: str = ""):
    """
    Send notification to configured channels.
    severity: info | warning | critical
    event_type: filter key from NOTIFY_FILTER

    ✅ CHANGED — event_type filter + parallel async sending
    """
    # Filter check — False hai toh skip
    if event_type and not NOTIFY_FILTER.get(event_type, True):
        log.debug(f"Notification suppressed by filter: {event_type}")
        return

    if not SLACK_ENABLED and not DISCORD_ENABLED:
        log.debug("Notifications skipped - no webhook URLs configured")
        return

    color_map = {"info": "#2196F3", "warning": "#FF9800", "critical": "#F44336"}
    emoji_map = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}
    color = color_map.get(severity, "#2196F3")
    emoji = emoji_map.get(severity, "ℹ️")

    full_title = f"{emoji} {title}"
    if repo:
        full_title += f" — `{repo}`"

    # ✅ CHANGED — parallel threads, dono simultaneously
    # Pehle sequential tha — ab ek fail ho toh doosra nahi rukta
    threads = []
    if SLACK_ENABLED:
        t = threading.Thread(target=_send_slack, args=(full_title, message, color), daemon=True)
        threads.append(t)
    if DISCORD_ENABLED:
        t = threading.Thread(target=_send_discord, args=(full_title, message, color), daemon=True)
        threads.append(t)

    for t in threads:
        t.start()


def _send_slack(title: str, message: str, color: str):
    try:
        payload = {
            "attachments": [{
                "color": color,
                "title": title,
                "text": message[:1000],
                "footer": "AI Repo Manager V3",
            }]
        }
        resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=5)
        if resp.status_code == 200:
            log.info("Slack notification sent")
        else:
            log.warning(f"Slack notification failed: {resp.status_code}")
    except Exception as e:
        log.error(f"Slack error: {e}")


def _send_discord(title: str, message: str, color: str):
    try:
        color_int = int(color.lstrip("#"), 16)
        payload = {
            "embeds": [{
                "title": title,
                "description": message[:2000],
                "color": color_int,
                "footer": {"text": "AI Repo Manager V3"}
            }]
        }
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
        if resp.status_code in (200, 204):
            log.info("Discord notification sent")
        else:
            log.warning(f"Discord notification failed: {resp.status_code}")
    except Exception as e:
        log.error(f"Discord error: {e}")


# ── Existing functions — NO CHANGE ────────────────────────────────────────────

def notify_secret_detected(repo: str, findings_count: int):
    notify(
        title="Secret Detected in Push",
        message=f"{findings_count} potential secret(s) found in a recent push. Immediate action required.",
        severity="critical",
        repo=repo,
        event_type="secret_detected"
    )


def notify_high_risk_pr(repo: str, pr_number: int, title: str):
    notify(
        title="High Risk PR Opened",
        message=f"PR #{pr_number}: {title}\nThis PR has been flagged as high risk and requires careful review.",
        severity="warning",
        repo=repo,
        event_type="high_risk_pr"
    )


def notify_health_degraded(repo: str, grade: str, score: int):
    notify(
        title="Repo Health Degraded",
        message=f"Repository health is now **{grade}** ({score}/100). Check the latest health report for recommendations.",
        severity="warning",
        repo=repo,
        event_type="health_degraded"
    )


def notify_ci_failure(repo: str, branch: str, error: str):
    notify(
        title="CI Failure Detected",
        message=f"Branch `{branch}` CI failed.\n{error[:200]}",
        severity="warning",
        repo=repo,
        event_type="ci_failure"
    )


# ── NEW functions ──────────────────────────────────────────────────────────────

def notify_new_issue(repo: str, issue_number: int, title: str, labels: list):
    # ✅ NEW — issues.py se call hoga jab issue open ho
    label_str = ", ".join(labels[:3]) if labels else "none"
    notify(
        title="New Issue Created",
        message=f"Issue #{issue_number}: {title}\nLabels: {label_str}",
        severity="info",
        repo=repo,
        event_type="new_issue"
    )


def notify_pr_opened(repo: str, pr_number: int, title: str, risk: str = "unknown"):
    # ✅ NEW — pull_request.py se call hoga jab PR open ho
    risk_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴", "unknown": "⏳"}.get(risk, "⏳")
    notify(
        title="New PR Opened",
        message=f"PR #{pr_number}: {title}\nRisk: {risk_emoji} {risk.capitalize()}",
        severity="warning" if risk == "high" else "info",
        repo=repo,
        event_type="pr_opened"
    )


def notify_stale_closed(repo: str, issue_number: int, title: str):
    # ✅ NEW — schedule.py se call hoga jab stale issue auto-close ho
    notify(
        title="Stale Issue Auto-Closed",
        message=f"Issue #{issue_number}: {title}\nClosed after {37} days of inactivity.",
        severity="info",
        repo=repo,
        event_type="stale_closed"
    )


def notify_vulnerability(repo: str, package: str, severity: str, ghsa_id: str):
    # ✅ NEW — HIGH bhejo, LOW skip (filter mein set hai)
    event_type = f"vulnerability_{severity.lower()}"
    notify(
        title=f"Vulnerability Found — {severity.upper()}",
        message=f"Package: `{package}`\nAdvisory: {ghsa_id}\nRun `pip install --upgrade {package}` to fix.",
        severity="critical" if severity.lower() == "high" else "info",
        repo=repo,
        event_type=event_type
    )
