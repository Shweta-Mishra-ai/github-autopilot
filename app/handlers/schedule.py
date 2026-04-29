"""
Schedule Handler - app/handlers/schedule.py
V3: Automated maintenance tasks on cron schedule.

FIXED (ruff F401): Removed unused `groq_ask, groq_text` imports.
FIXED (ruff E741): Renamed ambiguous `l` → `lbl` in list comprehension.
FIXED (ruff F821): Added `gh_put` to github client import (was called but not imported).
FIXED: Updated notify_stale_closed() call to pass days_inactive param (was missing).
"""

import os
from datetime import datetime, timedelta
from app.github.auth import get_installation_token
from app.github.client import gh_get, gh_post, gh_put, GitHubError
from app.github.notifications import notify_health_degraded, notify_stale_closed
from app.core.config import load_config
from app.core.logger import get_logger
from app.core.logger import EventLogger

log = get_logger(__name__)

STALE_DAYS = int(os.environ.get("STALE_ISSUE_DAYS", "30"))


def run_stale_check(repo: str, installation_id: int):
    """
    Flag issues with no activity for STALE_DAYS days.
    Posts a comment and adds 'stale' label.
    """
    log.info("schedule.stale_check.start", repo=repo)
    try:
        token = get_installation_token(installation_id)
        config = load_config(repo, token)

        cutoff = datetime.utcnow() - timedelta(days=STALE_DAYS)
        issues = gh_get(
            f"/repos/{repo}/issues?state=open&per_page=50&sort=updated&direction=asc",
            token
        )

        stale_count = 0
        for issue in issues:
            if "pull_request" in issue:
                continue

            updated_at = datetime.strptime(
                issue["updated_at"], "%Y-%m-%dT%H:%M:%SZ"
            )

            if updated_at < cutoff:
                _mark_stale(repo, issue, token, config)
                stale_count += 1

        log.info("schedule.stale_check.done", repo=repo, stale=stale_count)

    except Exception as e:
        log.error("schedule.stale_check.failed", repo=repo, error=str(e))


def _mark_stale(repo: str, issue: dict, token: str, config):
    """Post stale comment, add label, and auto-close if already stale for 7+ more days."""
    issue_number = issue["number"]
    title = issue.get("title", "")
    days_inactive = (
        datetime.utcnow() -
        datetime.strptime(issue["updated_at"], "%Y-%m-%dT%H:%M:%SZ")
    ).days

    # FIXED (E741): Renamed `l` → `lbl`
    labels = [lbl["name"] for lbl in issue.get("labels", [])]
    already_stale = "stale" in labels

    auto_close = already_stale and days_inactive >= (STALE_DAYS + 7)

    if auto_close:
        try:
            gh_post(f"/repos/{repo}/issues/{issue_number}/comments", token, {
                "body": (
                    f"## 🔒 Auto-Closed\n\n"
                    f"This issue has been inactive for **{days_inactive} days** "
                    f"and was marked stale. Closing automatically.\n\n"
                    f"Feel free to reopen if this is still relevant!\n\n"
                    f"> 🤖 Auto-closed by AI Repo Manager V4"
                )
            })
            # FIXED (F821): gh_put now imported above
            gh_put(f"/repos/{repo}/issues/{issue_number}", token, {"state": "closed"})
            log.info("schedule.stale_auto_closed",
                     repo=repo, issue=issue_number, days=days_inactive)

            # FIXED: Pass days_inactive to fix hardcoded 37 days bug
            try:
                notify_stale_closed(repo, issue_number, title, days_inactive)
            except Exception:
                pass

        except GitHubError as e:
            log.error("schedule.stale_close_failed",
                      repo=repo, issue=issue_number, error=str(e))
        return

    comment = f"""## 👴 Stale Issue

This issue has had no activity for **{days_inactive} days**.

It will be **automatically closed in 7 days** unless there is new activity.

- If this is still relevant, please leave a comment
- If this is resolved, please close it manually
- If this needs help, add the `help wanted` label

> 🤖 This is an automated message from AI Repo Manager V4
"""

    try:
        _ensure_label(repo, token, "stale", "cccccc", "No recent activity")
        gh_post(f"/repos/{repo}/issues/{issue_number}/labels", token,
                {"labels": ["stale"]})
        gh_post(f"/repos/{repo}/issues/{issue_number}/comments", token,
                {"body": comment + config.footer})
        log.info("schedule.stale_marked",
                 repo=repo, issue=issue_number, title=title[:50])
    except GitHubError as e:
        log.error("schedule.stale_mark_failed",
                  repo=repo, issue=issue_number, error=str(e))


def run_health_report(repo: str, installation_id: int):
    """Generate monthly health report and post as issue."""
    log.info("schedule.health_report.start", repo=repo)
    try:
        token = get_installation_token(installation_id)
        config = load_config(repo, token)

        repo_data    = gh_get(f"/repos/{repo}", token)
        all_issues   = gh_get(f"/repos/{repo}/issues?state=open&per_page=50", token)
        open_prs     = gh_get(f"/repos/{repo}/pulls?state=open&per_page=20", token)
        commits      = gh_get(f"/repos/{repo}/commits?per_page=30", token)
        contributors = gh_get(f"/repos/{repo}/contributors?per_page=10", token)

        open_issues = [i for i in all_issues if "pull_request" not in i]
        score = 100
        findings = []
        recommendations = []

        if len(open_issues) > 20:
            score -= 15
            findings.append(f"🔴 {len(open_issues)} open issues")
            recommendations.append("Triage and close old issues")
        elif len(open_issues) > 10:
            score -= 7
            findings.append(f"🟡 {len(open_issues)} open issues")
        else:
            findings.append(f"✅ {len(open_issues)} open issues")

        if len(open_prs) > 10:
            score -= 10
            findings.append(f"🔴 {len(open_prs)} open PRs")
            recommendations.append("Review and merge or close stale PRs")
        elif len(open_prs) > 5:
            score -= 5
            findings.append(f"🟡 {len(open_prs)} open PRs")
        else:
            findings.append(f"✅ {len(open_prs)} open PRs")

        if not repo_data.get("license"):
            score -= 8
            findings.append("🔴 No license file")
            recommendations.append("Add a LICENSE file")
        else:
            findings.append(f"✅ License: {repo_data['license'].get('name', '')}")

        if len(contributors) < 2:
            score -= 5
            findings.append("🟡 Single contributor")
            recommendations.append("Add CONTRIBUTING.md to attract contributors")
        else:
            findings.append(f"✅ {len(contributors)} contributors")

        if len(commits) < 5:
            score -= 10
            findings.append("🔴 Low recent commit activity")
        else:
            findings.append(f"✅ {len(commits)} recent commits")

        grade = (
            "A+" if score >= 90 else
            "A"  if score >= 80 else
            "B"  if score >= 70 else
            "C"  if score >= 60 else
            "D"  if score >= 50 else "F"
        )
        bar          = "█" * (score // 10) + "░" * (10 - score // 10)
        month        = datetime.utcnow().strftime("%B %Y")
        findings_md  = "\n".join(f"- {finding}" for finding in findings)
        recs_md      = "\n".join(f"{i+1}. {r}" for i, r in enumerate(recommendations[:5]))

        body = f"""## 🏥 Monthly Health Report — {month}

### Grade: **{grade}** ({score}/100)
`{bar}`

| Metric | Value |
|--------|-------|
| ⭐ Stars | {repo_data.get('stargazers_count', 0)} |
| 🍴 Forks | {repo_data.get('forks_count', 0)} |
| 📂 Open Issues | {len(open_issues)} |
| 🔀 Open PRs | {len(open_prs)} |
| 👥 Contributors | {len(contributors)} |
| 📝 Recent Commits | {len(commits)} |

### Findings
{findings_md}

{f"### 💡 Recommendations{chr(10)}{recs_md}" if recommendations else "### 💡 All good — keep it up!"}

---
> 🤖 Auto-generated monthly report by AI Repo Manager V4
"""

        gh_post(f"/repos/{repo}/issues", token, {
            "title": f"📊 Monthly Health Report — {month} — Grade: {grade}",
            "body": body + config.footer,
            "labels": ["health-report"]
        })

        if score < 70:
            try:
                notify_health_degraded(repo, grade, score)
            except Exception:
                pass

        log.info("schedule.health_report.done", repo=repo, grade=grade, score=score)

    except Exception as e:
        log.error("schedule.health_report.failed", repo=repo, error=str(e))


def run_dependency_report(repo: str, installation_id: int):
    """Check for outdated dependencies and post recommendations."""
    log.info("schedule.dependency_report.start", repo=repo)
    try:
        token  = get_installation_token(installation_id)
        config = load_config(repo, token)

        import base64
        try:
            data    = gh_get(f"/repos/{repo}/contents/requirements.txt", token)
            content = base64.b64decode(data["content"]).decode("utf-8")
        except Exception:
            log.info("schedule.dependency_report.no_requirements", repo=repo)
            return

        from app.security.dependencies import scan_requirements_txt, format_findings
        findings = scan_requirements_txt(content)

        if not findings:
            log.info("schedule.dependency_report.clean", repo=repo)
            return

        body = f"""## 📦 Weekly Dependency Security Report

{format_findings(findings)}

### Action Required
Run the following to update vulnerable packages:

```bash
pip install --upgrade {' '.join(f['package'] for f in findings[:5])}
```

---
> 🤖 Auto-generated weekly report by AI Repo Manager V4
"""

        gh_post(f"/repos/{repo}/issues", token, {
            "title": f"⚠️ {len(findings)} vulnerable dependencies found",
            "body": body + config.footer,
            "labels": ["security", "dependencies"]
        })

        log.info("schedule.dependency_report.done", repo=repo, findings=len(findings))

    except Exception as e:
        log.error("schedule.dependency_report.failed", repo=repo, error=str(e))


def _ensure_label(repo: str, token: str, name: str, color: str, description: str):
    """Create label if it doesn't exist."""
    try:
        gh_post(f"/repos/{repo}/labels", token, {
            "name": name,
            "color": color,
            "description": description
        })
    except Exception:
        pass  # Label already exists

def run_weekly_report(installation_id: int, repo: str):
    """
    Sprint 6: Weekly analytics digest.
    Called every Monday 9am UTC by APScheduler.
    Posts report to repo as a new issue.
    """
    log = EventLogger("schedule", repo=repo)
    try:
        token = get_installation_token(installation_id)
    except Exception as e:
        log.error(f"weekly_report auth failed: {e}")
        return

    try:
        from app.core.analytics import format_report_comment
        from app.github.notifications import notify_weekly_report

        body = format_report_comment(repo)

        # Post as issue for visibility
        from app.github.client import gh_post
        gh_post(f"/repos/{repo}/issues", token, {
            "title": f"📊 Weekly Bot Report — {__import__('datetime').date.today()}",
            "body": body,
            "labels": ["bot-report"],
        })

        # Also notify Discord
        from app.core.analytics import get_weekly_report
        data = get_weekly_report(repo)
        grade   = data["code_quality"]["grade"]
        merged  = data["prs"]["merged_today"]
        closed  = data["issues"]["closed_today"]
        notify_weekly_report(repo, grade, merged, closed)

        log.done(f"Weekly report posted for {repo}")
    except Exception as e:
        log.error(f"weekly_report failed: {e}")
