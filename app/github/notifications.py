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

from app import __version__

log = logging.getLogger(__name__)

# Read at call time, not import time.
#
# These were module-level constants evaluated once when the module was first
# imported, so a webhook URL set after that point — or changed, or provided by
# a test — was never seen. send_rich_discord() already read the environment on
# each call, so the same file disagreed with itself about when configuration
# is decided.
#
# The env var is the MASTER switch: it says whether the deployment has the
# channel at all. Per-repo config keys (notifications.slack / .discord) are
# overrides on top of it.


def slack_webhook_url() -> str:
    return os.environ.get("SLACK_WEBHOOK_URL", "").strip()


def discord_webhook_url() -> str:
    return os.environ.get("DISCORD_WEBHOOK_URL", "").strip()


def slack_enabled() -> bool:
    return bool(slack_webhook_url())


def discord_enabled() -> bool:
    return bool(discord_webhook_url())


NOTIFY_FILTER: dict[str, bool] = {
    "secret_detected": True,
    "vulnerability_high": True,
    "auto_merge": True,
    "high_risk_pr": True,
    "pr_opened": True,
    "new_issue": True,
    "all_providers_down": True,
    "vulnerability_low": False,
    "commit_lint": False,
    "pr_reviewed": False,
    "every_push": False,
}

_COLORS: dict[str, int] = {
    "critical": 15158332,
    "warning": 15105570,
    "info": 3447003,
    "success": 3066993,
}

_EMOJIS: dict[str, str] = {
    "critical": "🚨",
    "warning": "⚠️",
    "info": "ℹ️",
    "success": "✅",
}


# Maps an internal event_type to the repo-config key that governs it.
# Before this, the notifications.on_* keys existed only in DEFAULTS — a user
# who set on_secret_detected: false still got pinged on every secret.
_CONFIG_EVENT_KEYS = {
    "secret_detected": "on_secret_detected",
    "high_risk_pr": "on_high_risk_pr",
    "all_providers_down": "on_all_providers_down",
}


def _count(metric: str) -> None:
    """
    Record a delivery outcome.

    Sends happen on daemon threads, so a failing webhook only ever produced a
    log line in a thread nobody reads. Counting makes it visible on /metrics
    and /health, where "notifications stopped arriving" is actually diagnosable.
    """
    try:
        from app.core.metrics import metrics

        metrics.increment(metric)
    except Exception:  # metrics must never break a notification
        pass


def _event_allowed(event_type: str, config=None) -> bool:
    """
    Repo config wins over the module-level default filter.

    config is passed per call rather than held in module state: this process
    serves many repositories, and one repo's preferences must never leak into
    another's notifications.
    """
    if not event_type:
        return True
    if config is not None:
        key = _CONFIG_EVENT_KEYS.get(event_type)
        if key is not None:
            return bool(config.get("notifications", key, default=True))
    return NOTIFY_FILTER.get(event_type, True)


def notify(
    title: str,
    message: str,
    severity: str = "info",
    repo: str = "",
    event_type: str = "",
    fields: list[dict] | None = None,
    url: str = "",
    config=None,
):
    if not _event_allowed(event_type, config):
        log.debug(f"notification.suppressed event_type={event_type}")
        return

    slack_on = slack_enabled() and (
        config is None or config.get("notifications", "slack", default=True)
    )
    discord_on = discord_enabled() and (
        config is None or config.get("notifications", "discord", default=True)
    )
    if not slack_on and not discord_on:
        # Distinguish "no channel configured" from "a repo turned it off".
        # Both used to log the same line, so an operator whose webhook URL was
        # set but whose notifications never arrived had nothing to go on.
        if not slack_enabled() and not discord_enabled():
            log.debug("notification.skipped no_webhook_url_configured")
        else:
            log.info(
                f"notification.skipped_by_repo_config repo={repo or '?'} "
                f"event={event_type or '?'} — a webhook URL is set but this "
                f"repository's notifications.slack/.discord disable it"
            )
        return

    emoji = _EMOJIS.get(severity, "ℹ️")
    full_title = f"{emoji} {title}"
    if repo:
        full_title += f" — `{repo}`"

    threads: list[threading.Thread] = []

    if slack_on:
        t = threading.Thread(
            target=_send_slack,
            args=(full_title, message, severity),
            daemon=True,
        )
        threads.append(t)

    if discord_on:
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
        "warning": "#E67E22",
        "info": "#3498DB",
        "success": "#2ECC71",
    }
    try:
        payload = {
            "attachments": [
                {
                    "color": color_map.get(severity, "#3498DB"),
                    "title": title,
                    "text": message[:1000],
                    "footer": "GitHub Autopilot",
                    "ts": int(datetime.now(timezone.utc).timestamp()),
                }
            ]
        }
        resp = requests.post(slack_webhook_url(), json=payload, timeout=10)
        if resp.status_code == 200:
            _count("notifications.slack.sent")
            log.info("notification.slack_sent")
        else:
            _count("notifications.slack.failed")
            log.warning(
                f"notification.slack_failed status={resp.status_code} " f"body={resp.text[:200]}"
            )
    except Exception as e:
        _count("notifications.slack.failed")
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
            "title": title[:256],
            "description": message[:4096],
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "GitHub Autopilot"},
        }
        if url:
            embed["url"] = url
        if fields:
            embed["fields"] = [
                {
                    "name": str(f.get("name", ""))[:256],
                    "value": str(f.get("value", "\u200b"))[:1024],
                    "inline": bool(f.get("inline", True)),
                }
                for f in fields[:25]
            ]

        payload = {"embeds": [embed]}
        resp = requests.post(
            discord_webhook_url(),
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code in (200, 204):
            _count("notifications.discord.sent")
            log.info("notification.discord_sent")
        else:
            _count("notifications.discord.failed")
            log.warning(
                f"notification.discord_failed status={resp.status_code} body={resp.text[:200]}"
            )
    except Exception as e:
        _count("notifications.discord.failed")
        log.error(f"notification.discord_error: {e}")


def notify_secret_detected(repo: str, findings_count: int, config=None):
    notify(
        title="Secret Detected in Push",
        message=f"{findings_count} potential secret(s) found. Rotate credentials immediately.",
        severity="critical",
        repo=repo,
        event_type="secret_detected",
        config=config,
        fields=[
            {"name": "Findings", "value": str(findings_count), "inline": True},
            {"name": "Repository", "value": repo, "inline": True},
        ],
    )


def notify_high_risk_pr(repo: str, pr_number: int, title: str, config=None):
    notify(
        title="High Risk PR Opened",
        message=f"PR #{pr_number} flagged as HIGH risk.",
        severity="warning",
        repo=repo,
        event_type="high_risk_pr",
        config=config,
        fields=[
            {"name": "PR", "value": f"#{pr_number}", "inline": True},
            {"name": "Risk", "value": "🔴 HIGH", "inline": True},
            {"name": "Title", "value": title[:200]},
        ],
        url=f"https://github.com/{repo}/pull/{pr_number}",
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
            {"name": "CVE", "value": cve_id, "inline": True},
            {"name": "Fix", "value": f"`pip install --upgrade {package}`"},
        ],
    )


def notify_all_providers_down(config=None):
    try:
        from app.ai.circuit_breaker import status_all

        statuses = status_all()
        fields = [
            {
                "name": name,
                "value": f"{s['state']} — recovers in {s['recovers_in_seconds']}s"
                if s["recovers_in_seconds"]
                else s["state"],
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
        config=config,
        fields=fields,
    )


def test_discord() -> tuple[bool, str]:
    if not discord_enabled():
        return False, "DISCORD_WEBHOOK_URL environment variable is not set"

    try:
        payload = {
            "embeds": [
                {
                    "title": "✅ GitHub Autopilot — Discord Test",
                    "description": "Discord webhook is connected and working correctly!",
                    "color": _COLORS["success"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "footer": {"text": "GitHub Autopilot"},
                    "fields": [
                        {"name": "Status", "value": "Connected", "inline": True},
                        {"name": "Version", "value": __version__, "inline": True},
                    ],
                }
            ]
        }
        resp = requests.post(
            discord_webhook_url(),
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
    webhook_url = discord_webhook_url()
    if not webhook_url:
        return False, "DISCORD_WEBHOOK_URL not set"
    try:
        embed = {
            "title": title[:256],
            "description": description[:4096],
            "color": color,
        }
        if url:
            embed["url"] = url
        if fields:
            embed["fields"] = [
                {
                    "name": f.get("name", "")[:256],
                    "value": f.get("value", "")[:1024],
                    "inline": f.get("inline", False),
                }
                for f in fields[:25]
            ]
        payload = {"embeds": [embed]}
        r = requests.post(webhook_url, json=payload, timeout=10)
        ok = r.status_code in (200, 204)
        _count(f"notifications.discord.{'sent' if ok else 'failed'}")
        return ok, f"HTTP {r.status_code}"
    except Exception as e:
        _count("notifications.discord.failed")
        return False, str(e)


def send_rich_slack(
    title: str,
    description: str,
    color: str = "#3498DB",
    fields: list = None,
    url: str = "",
) -> tuple[bool, str]:
    """
    Slack counterpart to send_rich_discord, with the same (ok, detail) contract.

    It did not exist, so /notify — which explicitly accepts a Slack-only
    configuration — had nothing to call and sent Discord alone, while still
    reporting that Slack had been notified.
    """
    webhook_url = slack_webhook_url()
    if not webhook_url:
        return False, "SLACK_WEBHOOK_URL not set"
    try:
        attachment: dict = {
            "color": color,
            "title": title[:256],
            "text": description[:3000],
            "footer": "GitHub Autopilot",
            "ts": int(datetime.now(timezone.utc).timestamp()),
        }
        if url:
            attachment["title_link"] = url
        if fields:
            attachment["fields"] = [
                {
                    "title": str(f.get("name", ""))[:256],
                    "value": str(f.get("value", ""))[:1024],
                    "short": bool(f.get("inline", False)),
                }
                for f in fields[:10]
            ]
        r = requests.post(webhook_url, json={"attachments": [attachment]}, timeout=10)
        ok = r.status_code == 200
        _count(f"notifications.slack.{'sent' if ok else 'failed'}")
        return ok, f"HTTP {r.status_code}"
    except Exception as e:
        _count("notifications.slack.failed")
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
