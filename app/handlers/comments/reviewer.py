"""
app/handlers/comments/reviewer.py
Read-only review and analysis commands:
  /health, /version, /summarize, /ci, /budget, /report, /impact, /changelog
Plus shared helpers: _bump_version, _fetch_commits_since_tag.
"""

from __future__ import annotations

import logging
import re

from app.github.client import GitHubError
from app.github.helpers import fmt_error
import app.handlers.comments as hc


def gh_get(*a, **kw):
    return hc.gh_get(*a, **kw)


class RouterProxy:
    def __getattr__(self, name):
        return getattr(hc.router, name)


router = RouterProxy()

log = logging.getLogger(__name__)


# ── Shared helpers ────────────────────────────────────────────────────────────


def _fetch_commits_since_tag(repo: str, token: str, per_page: int = 20) -> tuple[list, str]:
    """
    Fetch recent commits and latest tag name.
    Shared by /changelog and /release to avoid duplicate GitHub API calls.
    Returns (commits, latest_tag).
    """
    tags = gh_get(f"/repos/{repo}/tags?per_page=1", token)
    commits = gh_get(f"/repos/{repo}/commits?per_page={per_page}", token)
    latest_tag = tags[0]["name"] if (isinstance(tags, list) and tags) else "v0.0.0"
    return (commits if isinstance(commits, list) else []), latest_tag


def _bump_version(version: str) -> str:
    """
    Increment patch segment: 'v1.2.3' → 'v1.2.4'.
    Falls back to 'v0.1.0' if parsing fails.
    """
    try:
        m = re.match(r"^(v?)(\d+)\.(\d+)\.(\d+)", version.strip())
        if m:
            prefix, major, minor, patch = m.groups()
            return f"{prefix}{major}.{minor}.{int(patch) + 1}"
    except Exception as e:
        log.debug(f"reviewer.bump_version_parse_failed version={version!r}: {e}")
    return "v0.1.0"


# ── Commands ──────────────────────────────────────────────────────────────────


def cmd_health(repo: str, token: str) -> str:
    """Repo health grade: issues, PRs, license, description."""
    try:
        repo_data = gh_get(f"/repos/{repo}", token)
        all_issues = gh_get(f"/repos/{repo}/issues?state=open&per_page=50", token)
        open_prs = gh_get(f"/repos/{repo}/pulls?state=open&per_page=20", token)

        open_issues = [i for i in all_issues if "pull_request" not in i]
        score = 100
        findings: list[str] = []
        recommendations: list[str] = []

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
        elif len(open_prs) > 5:
            score -= 5
            findings.append(f"🟡 {len(open_prs)} open PRs")
        else:
            findings.append(f"✅ {len(open_prs)} open PRs")

        if not repo_data.get("license"):
            score -= 8
            findings.append("🔴 No license")
            recommendations.append("Add LICENSE file")
        else:
            findings.append(f"✅ License: {repo_data['license'].get('name', '')}")

        if not repo_data.get("description"):
            score -= 5
            findings.append("🟡 No description")
        else:
            findings.append("✅ Description present")

        grade = (
            "A+"
            if score >= 90
            else "A"
            if score >= 80
            else "B"
            if score >= 70
            else "C"
            if score >= 60
            else "D"
            if score >= 50
            else "F"
        )
        bar = "█" * (score // 10) + "░" * (10 - score // 10)
        findings_md = "\n".join(f"- {f}" for f in findings)
        rec_section = (
            "\n### 💡 Recommendations\n"
            + "\n".join(f"{i + 1}. {r}" for i, r in enumerate(recommendations[:4]))
            if recommendations
            else "\n### 💡 All good!"
        )

        return (
            f"## 🏥 Repo Health — `{repo}`\n\n"
            f"### Grade: **{grade}** ({score}/100)\n"
            f"`{bar}`\n\n"
            f"### Findings\n{findings_md}"
            f"{rec_section}"
        )

    except Exception as exc:
        return fmt_error("Health Check Failed", exc)


def cmd_version(repo: str, token: str) -> str:
    """Show latest tag, release, and recent commits."""
    try:
        tags = gh_get(f"/repos/{repo}/tags?per_page=10", token)
        releases = gh_get(f"/repos/{repo}/releases?per_page=3", token)
        commits = gh_get(f"/repos/{repo}/commits?per_page=8", token)

        latest_tag = tags[0]["name"] if tags else "No tags yet"
        latest_release = releases[0]["name"] if releases else "No releases"
        tags_list = "\n".join(f"- `{t['name']}`" for t in tags[:5]) or "- No tags yet"
        commits_md = "\n".join(
            f"| `{c['sha'][:7]}` | {c['commit']['message'].split(chr(10))[0][:55]} |"
            for c in commits[:6]
        )

        return (
            f"## 🎛️ Version Status — `{repo}`\n\n"
            f"| | |\n|---|---|\n"
            f"| **Latest Tag** | `{latest_tag}` |\n"
            f"| **Latest Release** | `{latest_release}` |\n\n"
            f"### Recent Tags\n{tags_list}\n\n"
            f"### Recent Commits\n| SHA | Message |\n|-----|---------|"
            f"\n{commits_md}"
        )

    except Exception as exc:
        return fmt_error("Version check failed", exc)


def cmd_summarize(repo: str, issue_number: int, token: str) -> str:
    """Summarize a discussion thread."""
    try:
        from app.handlers.comments import router

        comments = gh_get(f"/repos/{repo}/issues/{issue_number}/comments?per_page=50", token)
        thread = "\n\n".join(f"@{c['user']['login']}: {c['body'][:300]}" for c in comments[:20])
        summary, _ = router.ask_text(
            "Senior engineer. Summarize GitHub discussions concisely.",
            f"Summarize this discussion thread:\n\n{thread[:3000]}",
            task="explain",
        )
        return f"## 📝 Thread Summary\n\n{summary}"
    except Exception as exc:
        return fmt_error("Summarize failed", exc)


def cmd_ci(context: str, repo: str = "", token: str = "") -> str:
    """Analyze a CI failure — from pasted log or latest failed run."""
    from app.handlers.comments import router

    ci_context = context.strip() if context else ""

    if not ci_context and repo and token:
        try:
            runs = gh_get(f"/repos/{repo}/actions/runs?status=failure&per_page=5", token)
            run_list = runs.get("workflow_runs", []) if isinstance(runs, dict) else []
            if not run_list:
                return (
                    "## ℹ️ No Recent CI Failures\n\n"
                    "No failed workflow runs found.\n\n"
                    "Paste your error log after `/ci` to analyze it directly."
                )
            latest = run_list[0]
            ci_context = (
                f"Workflow: {latest.get('name', 'unknown')}\n"
                f"Branch: {latest.get('head_branch', 'unknown')}\n"
                f"Status: {latest.get('conclusion', 'unknown')}\n"
                f"URL: {latest.get('html_url', '')}\n"
                f"Commit: {latest.get('head_sha', '')[:12]}\n"
                f"Message: {latest.get('head_commit', {}).get('message', '')[:200]}"
            )
        except Exception as exc:
            return (
                f"## ⚠️ Could not fetch CI runs\n\n`{str(exc)[:200]}`\n\n"
                "Paste your error log after `/ci` to analyze it directly."
            )
    elif not ci_context:
        return (
            "## ℹ️ No CI Context\n\n"
            "Paste the error log after `/ci`:\n```\n/ci\n<error log here>\n```"
        )

    try:
        r, _ = router.ask(
            "DevOps expert. Analyze CI failures precisely. JSON only.",
            f"""Analyze this CI failure:
{ci_context[:3000]}

Return JSON:
{{
  "root_cause": "exact reason in one sentence",
  "fix": "step-by-step commands to fix",
  "prevention": "how to prevent in future",
  "confidence": 0.85
}}""",
            task="ci_analysis",
        )

        if not isinstance(r, dict) or "root_cause" not in r:
            return f"## ⚠️ CI Analysis Incomplete\n\nRaw output:\n\n```\n{str(r)[:500]}\n```"

        return (
            f"## 🔴 CI Failure Analysis\n\n"
            f"**Root Cause:** {r.get('root_cause', 'Unknown')}\n\n"
            f"**Fix:**\n```\n{r.get('fix', 'No fix suggested')}\n```\n\n"
            f"**Prevention:** {r.get('prevention', 'N/A')}\n\n"
            f"*Confidence: {int(float(r.get('confidence', 0.85)) * 100)}%*"
        )

    except Exception as exc:
        log.error(f"cmd_ci LLM error: {exc}")
        return fmt_error("CI Analysis Failed", exc)


def cmd_budget() -> str:
    """Show today's AI token and cost usage."""
    try:
        from app.ai.metrics import format_budget_comment

        return format_budget_comment()
    except Exception as exc:
        return fmt_error("Budget check failed", exc)


def cmd_report(repo: str) -> str:
    """Show weekly analytics for this repo."""
    try:
        from app.core.analytics import record_command_used

        record_command_used(repo, "report")
    except Exception:
        pass  # analytics tracking is non-critical

    try:
        from app.core.analytics import format_report_comment

        report = format_report_comment(repo)
        if not report or not report.strip():
            return (
                "## 📊 No Data Yet\n\n"
                "No activity recorded for this repo yet. "
                "The report populates after the first PR, issue, or command."
            )
        return report
    except Exception as exc:
        err = str(exc).lower()
        if any(w in err for w in ("redis", "connection", "refused")):
            return "## ⚠️ Report Unavailable\n\nRedis is not reachable. Check `REDIS_URL` in Render."
        log.error(f"cmd_report error: {exc}")
        return fmt_error("Report failed", exc)


def cmd_impact(repo: str, issue_number: int, issue: dict, token: str) -> str:
    """Blast radius analysis for a PR."""
    if "pull_request" not in issue:
        return "## ℹ️ `/impact` only works on Pull Requests."

    try:
        from app.handlers.comments import router
        from app.handlers.pull_request import _blast_radius

        files = gh_get(f"/repos/{repo}/pulls/{issue_number}/files", token)
        blast = _blast_radius(files)
        filenames = [f["filename"] for f in files[:15]]

        r, _ = router.ask(
            "Senior architect. Analyze PR impact on system. JSON only.",
            f"""Analyze blast radius of these file changes:
{chr(10).join(filenames)}

Return JSON:
{{
  "summary": "one sentence overall impact",
  "affected_systems": ["system1"],
  "breaking_change_risk": "low|medium|high",
  "requires_migration": false,
  "review_priority": "low|medium|high",
  "notes": "any considerations"
}}""",
            task="arch",
        )

        bc_risk = r.get("breaking_change_risk", "low")
        bc_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(bc_risk, "🟡")
        migration = "⚠️ Yes" if r.get("requires_migration") else "✅ No"
        systems = ", ".join(f"`{s}`" for s in r.get("affected_systems", [])[:5])
        notes_sec = f"\n> ℹ️ {r.get('notes')}" if r.get("notes") else ""

        return (
            f"## 💥 Blast Radius — PR #{issue_number}\n\n"
            f"**Summary:** {r.get('summary', '')}\n\n"
            f"### Layers Affected\n{blast}\n\n"
            f"### Impact Assessment\n| | |\n|---|---|\n"
            f"| **Breaking Change Risk** | {bc_emoji} {bc_risk.capitalize()} |\n"
            f"| **Requires Migration** | {migration} |\n"
            f"| **Review Priority** | `{r.get('review_priority', 'medium')}` |\n"
            f"| **Affected Systems** | {systems or 'none identified'} |"
            f"{notes_sec}"
        )

    except Exception as exc:
        return fmt_error("Impact analysis failed", exc)


def cmd_changelog(repo: str, token: str) -> str:
    """Generate a Keep-a-Changelog entry from recent commits."""
    from app.handlers.comments import router

    try:
        commits, latest_tag = _fetch_commits_since_tag(repo, token)

        if not commits:
            return "## ℹ️ No Commits Found\n\nNo commits in this repository yet."

        commit_list = "\n".join(
            f"- {c['commit']['message'].split(chr(10))[0][:120]}" for c in commits[:15]
        )

        if not commit_list.strip():
            return f"## ℹ️ No New Commits\n\nNo new commits since `{latest_tag}`."

        changelog, _ = router.ask_text(
            "Technical writer. Generate a CHANGELOG entry. Keep a Changelog format.",
            f"""Generate CHANGELOG.md entry for version after {latest_tag}.

Commits:
{commit_list}

Format:
## [X.Y.Z] - YYYY-MM-DD
### Added
- ...
### Changed
- ...
### Fixed
- ...

Skip empty sections. Use today's date.""",
            task="changelog",
        )

        if not changelog or not changelog.strip():
            return "## ⚠️ Changelog generation returned empty response. Try again."

        return (
            f"## 📋 CHANGELOG Entry\n\n"
            f"```markdown\n{changelog.strip()}\n```\n\n"
            f"*Copy into your `CHANGELOG.md` before the previous entry.*"
        )

    except GitHubError as exc:
        return fmt_error("Changelog failed (GitHub API)", exc)
    except Exception as exc:
        log.error(f"cmd_changelog error: {exc}")
        return fmt_error("Changelog generation failed", exc)
