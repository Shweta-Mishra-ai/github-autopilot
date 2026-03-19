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

# ✅ NEW — Notification filter map
# Sirf actionable alerts bhejo — har event pe ping = ignored notifications
# True = bhejo, False = skip
NOTIFY_FILTER = {
    "secret_detected": True,       # 🚨 ALWAYS — immediate action needed
    "vulnerability_high": True,    # 🚨 ALWAYS — security risk
    "vulnerability_low": False,    # ❌ SKIP — too noisy, low priority
    "high_risk_pr": True,          # ⚠️ reviewer ko batana zaroori
    "health_degraded": True,       # ⚠️ repo deteriorating
    "ci_failure": True,            # ⚠️ pipeline broken
    "stale_closed": False,         # ❌ SKIP — routine maintenance, not urgent
    "pr_reviewed": False,          # ❌ SKIP — too noisy on active repos
    "commit_lint": False,          # ❌ SKIP — developer ka kaam, alert nahi
    "health_on_every_push": False, # ❌ SKIP — weekly report kaafi hai
}


def notify(title: str, message: str, severity: str = "info", repo: str = "", event_type: str = ""):
    """
    Send notification to configured channels.
    severity: info | warning | critical
    event_type: filter key from NOTIFY_FILTER — agar False hai toh skip hoga

    ✅ CHANGED — event_type filter add kiya
    Pehle har event pe notification jata tha — ab sirf important wale jayenge
    """
    # ✅ NEW — Filter check
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

    # ✅ NEW — Async send karo dono channels pe parallel
    # Pehle sequential tha — Slack fail hota toh Discord bhi delay hota
    # Ab dono simultaneously jayenge, ek fail ho toh doosra nahi rukta
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


# ✅ CHANGED — event_type parameter add kiya har function mein filter ke liye

def notify_secret_detected(repo: str, findings_count: int):
    notify(
        title="Secret Detected in Push",
        message=f"{findings_count} potential secret(s) found in a recent push. Immediate action required.",
        severity="critical",
        repo=repo,
        event_type="secret_detected"  # ← FILTER: always sends
    )


def notify_high_risk_pr(repo: str, pr_number: int, title: str):
    notify(
        title="High Risk PR Opened",
        message=f"PR #{pr_number}: {title}\nThis PR has been flagged as high risk and requires careful review.",
        severity="warning",
        repo=repo,
        event_type="high_risk_pr"  # ← FILTER: always sends
    )


def notify_health_degraded(repo: str, grade: str, score: int):
    notify(
        title="Repo Health Degraded",
        message=f"Repository health is now **{grade}** ({score}/100). Check the latest health report for recommendations.",
        severity="warning",
        repo=repo,
        event_type="health_degraded"  # ← FILTER: always sends
    )


def notify_ci_failure(repo: str, branch: str, error: str):
    notify(
        title="CI Failure Detected",
        message=f"Branch `{branch}` CI failed.\n{error[:200]}",
        severity="warning",
        repo=repo,
        event_type="ci_failure"  # ← FILTER: always sends
    )


# ✅ NEW — Vulnerability alert with severity filter
# HIGH = bhejo, LOW = skip (filter mein set hai)
def notify_vulnerability(repo: str, package: str, severity: str, ghsa_id: str):
    event_type = f"vulnerability_{severity.lower()}"
    notify(
        title=f"Vulnerability Found — {severity.upper()}",
        message=f"Package: `{package}`\nAdvisory: {ghsa_id}\nRun `pip install --upgrade {package}` to fix.",
        severity="critical" if severity.lower() == "high" else "info",
        repo=repo,
        event_type=event_type  # ← vulnerability_high = send, vulnerability_low = skip
    )
