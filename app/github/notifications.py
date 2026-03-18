"""
Notifications - app/github/notifications.py
V3: Slack and Discord webhook notifications.
Configure via environment variables:
  SLACK_WEBHOOK_URL - Slack incoming webhook URL
  DISCORD_WEBHOOK_URL - Discord webhook URL
"""

import os
import requests
import logging

log = logging.getLogger(__name__)

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

SLACK_ENABLED = bool(SLACK_WEBHOOK_URL)
DISCORD_ENABLED = bool(DISCORD_WEBHOOK_URL)


def notify(title: str, message: str, severity: str = "info", repo: str = ""):
    """
    Send notification to configured channels.
    severity: info | warning | critical
    Only sends if webhook URLs are configured.
    """
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

    if SLACK_ENABLED:
        _send_slack(full_title, message, color)

    if DISCORD_ENABLED:
        _send_discord(full_title, message, color)


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


def notify_secret_detected(repo: str, findings_count: int):
    notify(
        title="Secret Detected in Push",
        message=f"{findings_count} potential secret(s) found in a recent push. Immediate action required.",
        severity="critical",
        repo=repo
    )


def notify_high_risk_pr(repo: str, pr_number: int, title: str):
    notify(
        title="High Risk PR Opened",
        message=f"PR #{pr_number}: {title}\nThis PR has been flagged as high risk and requires careful review.",
        severity="warning",
        repo=repo
    )


def notify_health_degraded(repo: str, grade: str, score: int):
    notify(
        title="Repo Health Degraded",
        message=f"Repository health is now **{grade}** ({score}/100). Check the latest health report for recommendations.",
        severity="warning",
        repo=repo
    )


def notify_ci_failure(repo: str, branch: str, error: str):
    notify(
        title="CI Failure Detected",
        message=f"Branch `{branch}` CI failed.\n{error[:200]}",
        severity="warning",
        repo=repo
    )
