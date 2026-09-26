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
from ._client import gh_get, router  # noqa: F401  (re-exported: tests patch these names)


log = logging.getLogger(__name__)


# ── Shared helpers ────────────────────────────────────────────────────────────


def commits_since(commits: list, tag_sha: str | None) -> list:
    """
    Truncate a newest-first commit list at `tag_sha`, exclusive.

    Returns `commits` unchanged when the tag is not in the window — the caller
    fetches a bounded page, so a tag older than that page is simply not visible
    and every commit in hand is genuinely unreleased as far as we can tell.
    Pure, so both /changelog and /release can be tested against it directly.
    """
    if not tag_sha:
        return commits
    for idx, c in enumerate(commits):
        if isinstance(c, dict) and c.get("sha") == tag_sha:
            return commits[:idx]
    return commits


def _fetch_commits_since_tag(repo: str, token: str, per_page: int = 20) -> tuple[list, str]:
    """
    Fetch the commits made SINCE the latest tag, plus that tag's name.

    The name has always said "since tag" and the caller has always believed it:
    /changelog renders "No new commits since <tag>" when the list is empty, and
    otherwise asks the model for "the entry for the version after <tag>". But
    the tag was only ever interpolated into the prompt — the list itself was the
    last `per_page` commits on the default branch, released ones included — so
    every /changelog re-described work that had already shipped in <tag>, and
    the empty branch was unreachable.
    """
    tags = gh_get(f"/repos/{repo}/tags?per_page=1", token)
    commits = gh_get(f"/repos/{repo}/commits?per_page={per_page}", token)
    has_tag = isinstance(tags, list) and tags and isinstance(tags[0], dict)
    latest_tag = tags[0].get("name", "v0.0.0") if has_tag else "v0.0.0"
    tag_sha = (tags[0].get("commit") or {}).get("sha") if has_tag else None
    return commits_since(commits if isinstance(commits, list) else [], tag_sha), latest_tag


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
    """
    Summarize a discussion thread.

    Delegates to intelligence.summarizer, which was a second implementation of
    this exact function that nothing called. That one is the better of the two:
    this one built the thread with `c['user']['login']` and `c['body'][:300]`,
    so a comment from a deleted account (GitHub sends `"user": null`) or with a
    null body raised inside the try and answered "Summarize failed". It also
    asks for a structured answer — what the issue is about, key points, current
    status, action items — instead of an unguided "summarize this".
    """
    try:
        from app.intelligence.summarizer import summarize_issue_thread

        comments = gh_get(f"/repos/{repo}/issues/{issue_number}/comments?per_page=50", token)
        if not isinstance(comments, list):
            comments = []

        issue = {}
        try:
            issue = gh_get(f"/repos/{repo}/issues/{issue_number}", token) or {}
        except Exception as exc:
            log.debug(f"cmd_summarize.title_unavailable: {exc}")

        summary = summarize_issue_thread(comments, issue)
        if not summary.strip():
            return "## 📝 Thread Summary\n\nCould not summarize this thread right now."
        return f"## 📝 Thread Summary\n\n{summary}"
    except Exception as exc:
        return fmt_error("Summarize failed", exc)


def cmd_ci(context: str, repo: str = "", token: str = "") -> str:
    """Analyze a CI failure — from pasted log or latest failed run."""
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
        from app.ai.guarded import degraded_comment, guarded_ask, is_degraded

        r, _verdict = guarded_ask(
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
            response_type="ci",
        )

        if is_degraded(r):
            return degraded_comment(r, "CI analysis")

        if not isinstance(r, dict) or "root_cause" not in r:
            return f"## ⚠️ CI Analysis Incomplete\n\nRaw output:\n\n```\n{str(r)[:500]}\n```"

        # A model that answers "confidence": "high" used to raise ValueError
        # here — after a perfectly good root cause and fix had been produced —
        # and the blanket handler below replaced all of it with "CI Analysis
        # Failed". Omit the line rather than lose the analysis, and rather than
        # print a percentage the model never gave.
        try:
            conf = max(0.0, min(1.0, float(r.get("confidence", 0.85))))
            conf_line = f"\n\n*Confidence: {int(conf * 100)}%*"
        except (TypeError, ValueError):
            conf_line = ""

        return (
            f"## 🔴 CI Failure Analysis\n\n"
            f"**Root Cause:** {r.get('root_cause', 'Unknown')}\n\n"
            f"**Fix:**\n```\n{r.get('fix', 'No fix suggested')}\n```\n\n"
            f"**Prevention:** {r.get('prevention', 'N/A')}"
            f"{conf_line}"
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
        from app.handlers.pull_request import _blast_radius

        from app.github.helpers import pr_files

        files = pr_files(repo, issue_number, token, get=gh_get)
        blast = _blast_radius(files)
        filenames = [f["filename"] for f in files[:15]]

        from app.ai.guarded import degraded_comment, guarded_ask, is_degraded

        from app.github.helpers import repo_file_context

        r, _verdict = guarded_ask(
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
            response_type="impact",
            # The whole tree, not the PR's files: naming files outside the
            # change is what a blast-radius answer is for.
            context=repo_file_context(
                repo, token, extra=[f.get("filename") for f in files], get=gh_get
            ),
        )

        if is_degraded(r):
            return degraded_comment(r, "impact analysis")

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

    try:
        commits, latest_tag = _fetch_commits_since_tag(repo, token)

        # Empty now means "nothing since the tag" — which is the case the old
        # unreachable branch below was written for. It said "no commits in this
        # repository yet", which was only ever true for an empty repo and is
        # the wrong thing to tell someone who has simply already released.
        if not commits:
            return f"## ℹ️ No New Commits\n\nNothing on the default branch since `{latest_tag}`."

        commit_list = "\n".join(
            f"- {c['commit']['message'].split(chr(10))[0][:120]}" for c in commits[:15]
        )

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
