"""
Notifications - app/github/notifications.py
V3: Send alerts to Slack and/or Discord.
Configure via .ai-repo-manager.yml or environment variables.
"""

import os
import requests
from app.core.logger import get_logger

log = get_logger(__name__)

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")


def notify(title: str, message: str, severity: str = "info", repo: str = ""):
    """
    Send notification to configured channels.
    severity: info | warning | critical
    """
    color_map = {
        "info":     "#2196F3",
        "warning":  "#FF9800",
        "critical": "#F44336",
    }
    color = color_map.get(severity, "#2196F3")
    emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(severity, "ℹ️")
    full_title = f"{emoji} {title}"
    if repo:
        full_title += f" — `{repo}`"

    if SLACK_WEBHOOK_URL:
        _send_slack(full_title, message, color)

    if DISCORD_WEBHOOK_URL:
        _send_discord(full_title, message, color)


def _send_slack(title: str, message: str, color: str):
    try:
        payload = {
            "attachments": [{
                "color": color,
                "title": title,
                "text": message,
                "footer": "AI Repo Manager V3",
            }]
        }
        resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=5)
        if resp.status_code == 200:
            log.info("notification.slack.sent")
        else:
            log.warning("notification.slack.failed", status=resp.status_code)
    except Exception as e:
        log.error("notification.slack.error", error=str(e))


def _send_discord(title: str, message: str, color: str):
    try:
        # Convert hex color to int for Discord
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
            log.info("notification.discord.sent")
        else:
            log.warning("notification.discord.failed", status=resp.status_code)
    except Exception as e:
        log.error("notification.discord.error", error=str(e))


def notify_secret_detected(repo: str, findings_count: int):
    notify(
        title="Secret Detected in Push",
        message=f"{findings_count} potential secret(s) found in a recent push to `{repo}`. Immediate action required.",
        severity="critical",
        repo=repo
    )


def notify_high_risk_pr(repo: str, pr_number: int, title: str):
    notify(
        title="High Risk PR Opened",
        message=f"PR #{pr_number}: {title}\nThis PR has been flagged as high risk and requires review.",
        severity="warning",
        repo=repo
    )


def notify_health_degraded(repo: str, grade: str, score: int):
    notify(
        title="Repo Health Degraded",
        message=f"Repository health is now **{grade}** ({score}/100). Review recommendations in the latest health report.",
        severity="warning",
        repo=repo
    )

