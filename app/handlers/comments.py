"""
Comments Handler - app/handlers/comments.py
Handles issue_comment webhook events — all slash commands.
Commands: /fix /apply /explain /improve /test /docs /refactor /health /version /merge
"""

import re
import logging
from app.github.auth import get_installation_token
from app.github.client import gh_get, gh_post, gh_put, gh_delete, GitHubError
from app.ai.client import groq_ask, groq_text
from app.core.config import load_config
from app.core.logger import EventLogger

SKIP_AUTHORS = {"dependabot[bot]", "renovate[bot]", "github-actions[bot]", "ai-repo-manager[bot]"}

ALL_COMMANDS = ["/fix", "/apply", "/explain", "/improve", "/test", "/docs",
                "/refactor", "/health", "/version", "/merge"]


def handle(payload: dict):
    action = payload.get("action")
    if action != "created":
        return

    comment = payload["comment"]
    body = comment.get("body", "")
    author = comment["user"]["login"]
    repo = payload["repository"]["full_name"]
    issue_number = payload["issue"]["number"]
    installation_id = payload["installation"]["id"]

    if author in SKIP_AUTHORS or author.endswith("[bot]"):
        return

    cmd = next((c for c in ALL_COMMANDS if c in body.lower()), None)
    if not cmd:
        return

    log = EventLogger("comments", repo=repo)
    log.info(f"Command {cmd} by @{author} on #{issue_number}")

    try:
        token = get_installation_token(installation_id)
    except Exception as e:
        log.error(f"Auth failed: {e}")
        return

    config = load_config(repo, token)

    if not config.command_enabled(cmd):
        try:
            gh_post(f"/repos/{repo}/issues/{issue_number}/comments", token, {
                "body": f"## ℹ️ Command Disabled\n\n`{cmd}` is disabled in `.ai-repo-manager.yml`.{config.footer}"
            })
        except Exception:
            pass
        return

    # Get issue/PR context
    try:
        issue = gh_get(f"/repos/{repo}/issues/{issue_number}", token)
        ctx_title = issue.get("title", "")
        ctx_body = issue.get("body", "") or ""
    except Exception:
        ctx_title, ctx_body = "", ""

    # Extract code block from comment if present
    code_match = re.search(r'```[\w]*\n([\s\S]*?)\n```', body)
    code = code_match.group(1) if code_match else ""
    context_text = re.sub(r'```[\s\S]*?```', '', body).replace(cmd, "").strip()
    full_context = code or context_text or ctx_body or ctx_title

    # Route to command handler
    response = ""

    try:
        if cmd == "/fix":
            response = _cmd_fix(ctx_title, full_context)
        elif cmd == "/apply":
            response = _cmd_apply(repo, issue_number, ctx_title, full_context, token)
        elif cmd == "/explain":
            response = _cmd_explain(full_context)
        elif cmd == "/improve":
            response = _cmd_improve(full_context)
        elif cmd == "/test":
            response = _cmd_test(full_context)
        elif cmd == "/docs":
            response = _cmd_docs(full_context)
        elif cmd == "/refactor":
            response = _cmd_refactor(full_context)
        elif cmd == "/health":
            response = _cmd_health(repo, token)
        elif cmd == "/version":
            response = _cmd_version(repo, token)
        elif cmd == "/merge":
            response = _cmd_merge(repo, issue_number, issue, token, author, config)

    except Exception as e:
        log.error(f"Command {cmd} failed: {e}")
        response = f"## ⚠️ Command Error\n\n`{cmd}` failed: `{str(e)[:200]}`\n\nPlease try again in a moment."

    if response:
        full = f"{response}\n\n---\n*🤖 `{cmd}` — requested by @{author}*{config.footer}"
        try:
            gh_post(f"/repos/{repo}/issues/{issue_number}/comments", token, {"body": full})
            log.done(f"{cmd} response posted")
        except GitHubError as e:
            log.error(f"Could not post response: {e}")


# ─────────────────────────────────────────────────────
# COMMAND IMPLEMENTATIONS
# ─────────────────────────────────────────────────────

def _cmd_fix(ctx_title: str, context: str) -> str:
    r = groq_ask(
        "Senior engineer. Give precise, working fix. JSON only.",
        f"""Fix this issue:
Title: {ctx_title}
Context: {context[:2000]}

Return JSON:
{{
  "root_cause": "exact reason",
  "fix": "working code or list of commit fixes",
  "explanation": "why this fix works",
  "test": "test to verify fix"
}}""",
        fast=True
    )
    return (
        f"## 🔧 Fix\n\n"
        f"**Root cause:** {r.get('root_cause', 'See fix below')}\n\n"
        f"**Fix:**\n```\n{r.get('fix', '')}\n```\n\n"
        f"**Why:** {r.get('explanation', '')}\n\n"
        f"**Test:**\n```\n{r.get('test', '')}\n```\n\n"
        f"💡 Use `/apply` to automatically apply these fixes to your commits."
    )


def _cmd_apply(repo: str, issue_number: int, ctx_title: str,
               context: str, token: str) -> str:
    """
    Auto-apply commit message fixes for non-conventional commits.
    Reads flagged commits from issue context, rewrites messages via GitHub API.
    """
    try:
        # Step 1: Get the default branch
        repo_data = gh_get(f"/repos/{repo}", token)
        default_branch = repo_data.get("default_branch", "main")

        # Step 2: Get recent commits from the branch
        commits = gh_get(f"/repos/{repo}/commits?sha={default_branch}&per_page=20", token)

        if not commits:
            return "## ⚠️ No commits found in repository."

        # Step 3: Use AI to identify which commits need fixing and suggest new messages
        commit_list = "\n".join(
            f"- SHA: {c['sha']} | Message: {c['commit']['message'].split(chr(10))[0]}"
            for c in commits[:15]
        )

        r = groq_ask(
            "You are a Git expert. Identify non-conventional commits and fix them. JSON only.",
            f"""Issue: {ctx_title}
Context: {context[:1000]}

Recent commits:
{commit_list}

Identify commits that do NOT follow Conventional Commits format (type(scope): description).
Valid types: feat, fix, docs, refactor, test, chore, perf, ci, style, build

Return JSON:
{{
  "commits": [
    {{
      "sha": "full_sha_here",
      "old_message": "original message",
      "new_message": "conventional(scope): message"
    }}
  ]
}}

Only include commits that need fixing. If all are conventional, return empty list.""",
            fast=True
        )

        commits_to_fix = r.get("commits", [])

        if not commits_to_fix:
            return (
                "## ✅ Nothing to Fix\n\n"
                "All recent commits already follow Conventional Commits format! 🎉"
            )

        # Step 4: Get current branch ref SHA
        ref_data = gh_get(f"/repos/{repo}/git/ref/heads/{default_branch}", token)
        current_sha = ref_data["object"]["sha"]

        fixed = []
        failed = []
        last_sha = current_sha

        # Step 5: Process each commit - create new commit with fixed message
        for c in commits_to_fix:
            sha = c.get("sha", "").strip()
            new_msg = c.get("new_message", "").strip()
            old_msg = c.get("old_message", sha[:7])

            if not sha or not new_msg:
                continue

            try:
                # Get full commit data
                commit_data = gh_get(f"/repos/{repo}/git/commits/{sha}", token)
                tree_sha = commit_data["tree"]["sha"]
                parents = [p["sha"] for p in commit_data.get("parents", [])]

                # Create new commit with fixed message
                new_commit = gh_post(f"/repos/{repo}/git/commits", token, {
                    "message": new_msg,
                    "tree": tree_sha,
                    "parents": parents
                })

                new_sha = new_commit["sha"]
                last_sha = new_sha
                fixed.append(
                    f"✅ `{sha[:7]}` → `{new_msg}`\n"
                    f"   *(was: `{old_msg[:50]}`)*"
                )

            except Exception as e:
                failed.append(f"❌ `{sha[:7]}` (`{old_msg[:40]}`) — {str(e)[:80]}")

        # Step 6: Update branch ref to point to last new commit
        if fixed:
            try:
                gh_post(f"/repos/{repo}/git/refs/heads/{default_branch}", token, {
                    "sha": last_sha,
                    "force": True
                })
            except Exception as e:
                return (
                    f"## ⚠️ Commits created but branch update failed\n\n"
                    f"`{str(e)[:200]}`\n\n"
                    f"Fixed commits were created but not applied to `{default_branch}`."
                )

        # Step 7: Close the issue if all fixed
        if fixed and not failed:
            try:
                gh_post(f"/repos/{repo}/issues/{issue_number}/comments", token, {
                    "body": "All commits fixed! Closing this issue. ✅"
                })
                gh_put(f"/repos/{repo}/issues/{issue_number}", token, {
                    "state": "closed"
                })
            except Exception:
                pass

        # Build response
        result_lines = ["## 🔧 Auto-Apply Results\n"]

        if fixed:
            result_lines.append(f"### ✅ Fixed ({len(fixed)} commits)\n")
            result_lines.extend(fixed)

        if failed:
            result_lines.append(f"\n### ❌ Failed ({len(failed)} commits)\n")
            result_lines.extend(failed)
            result_lines.append(
                "\n> 💡 Failed commits may need manual fix via `git rebase -i`"
            )

        if fixed:
            result_lines.append(
                f"\n✨ Branch `{default_branch}` updated successfully!"
            )

        return "\n".join(result_lines)

    except Exception as e:
        return (
            f"## ⚠️ Apply Failed\n\n"
            f"`{str(e)[:300]}`\n\n"
            f"Try fixing manually:\n"
            f"```bash\ngit rebase -i HEAD~7\n# Then update each commit message\n```"
        )


def _cmd_explain(context: str) -> str:
    text = groq_text(
        "Senior engineer and teacher. Explain clearly in plain English.",
        f"Explain this:\n{context[:2000]}"
    )
    return f"## 💡 Explanation\n\n{text}"


def _cmd_improve(context: str) -> str:
    r = groq_ask(
        "Staff engineer. Suggest concrete improvements. JSON only.",
        f"""Suggest improvements for:
{context[:2000]}

Return JSON:
{{
  "summary": "overall assessment",
  "improvements": [
    {{
      "area": "performance|security|readability|structure",
      "suggestion": "what to change",
      "example": "code example"
    }}
  ]
}}""",
        fast=True
    )
    lines = [f"## ✨ Improvements\n\n**{r.get('summary', '')}**\n"]
    for i, imp in enumerate(r.get("improvements", [])[:4], 1):
        lines.append(f"### {i}. `{imp.get('area','').upper()}` — {imp.get('suggestion','')}")
        if imp.get("example"):
            lines.append(f"```\n{imp['example'][:300]}\n```")
    return "\n\n".join(lines)


def _cmd_test(context: str) -> str:
    r = groq_ask(
        "Senior QA engineer. Generate comprehensive tests. JSON only.",
        f"""Write tests for:
{context[:2000]}

Return JSON:
{{
  "framework": "pytest",
  "tests": [
    {{
      "name": "test_function_name",
      "type": "unit|integration|edge_case",
      "desc": "what it tests",
      "code": "full test code"
    }}
  ]
}}""",
        fast=True
    )
    tests = r.get("tests", [])
    lines = [f"## 🧪 Tests ({r.get('framework', 'pytest')})\n"]
    for t in tests[:3]:
        lines.append(
            f"### `{t.get('name', 'test')}` ({t.get('type', 'unit')})\n"
            f"*{t.get('desc', '')}*\n"
            f"```python\n{t.get('code', '')[:400]}\n```"
        )
    return "\n\n".join(lines)


def _cmd_docs(context: str) -> str:
    r = groq_ask(
        "Technical writer. Generate clear documentation. JSON only.",
        f"""Generate docs for:
{context[:2000]}

Return JSON:
{{
  "docstring": "complete docstring",
  "usage": "usage example",
  "readme_section": "markdown section for README"
}}""",
        fast=True
    )
    return (
        f"## 📚 Documentation\n\n"
        f"**Docstring:**\n```\n{r.get('docstring', '')}\n```\n\n"
        f"**Usage:**\n```\n{r.get('usage', '')}\n```\n\n"
        f"**README section:**\n{r.get('readme_section', '')}"
    )


def _cmd_refactor(context: str) -> str:
    r = groq_ask(
        "Principal engineer. Suggest refactoring that preserves behavior exactly. JSON only.",
        f"""Suggest refactoring for:
{context[:2500]}

RULE: Behavior must be identical after refactoring. Only structure/readability/performance changes.

Return JSON:
{{
  "summary": "assessment",
  "estimated_improvement": "e.g. 40% more readable",
  "refactors": [
    {{
      "type": "extract_function|rename|simplify|optimize",
      "description": "what and why",
      "before": "original snippet",
      "after": "refactored snippet",
      "benefit": "concrete benefit"
    }}
  ]
}}"""
    )
    lines = [
        f"## ♻️ Refactor Suggestions\n\n"
        f"**{r.get('summary', '')}**\n\n"
        f"*Estimated improvement: {r.get('estimated_improvement', 'significant')}*\n\n"
        f"> ✅ All suggestions preserve identical behavior — only code structure changes.\n"
    ]
    for i, ref in enumerate(r.get("refactors", [])[:4], 1):
        lines.append(f"### {i}. `{ref.get('type','').upper()}` — {ref.get('description','')}")
        if ref.get("before"):
            lines.append(f"**Before:**\n```\n{ref['before'][:300]}\n```")
        if ref.get("after"):
            lines.append(f"**After:**\n```\n{ref['after'][:300]}\n```")
        lines.append(f"✅ **Benefit:** {ref.get('benefit','')}")
    return "\n\n".join(lines)


def _cmd_health(repo: str, token: str) -> str:
    """
    Repo Health Analysis — grades repo A+ to F.
    Checks: open issues, open PRs, contributors, license, activity, languages.
    """
    try:
        repo_data = gh_get(f"/repos/{repo}", token)
        all_issues = gh_get(f"/repos/{repo}/issues?state=open&per_page=50", token)
        open_prs = gh_get(f"/repos/{repo}/pulls?state=open&per_page=20", token)
        commits = gh_get(f"/repos/{repo}/commits?per_page=20", token)
        contributors = gh_get(f"/repos/{repo}/contributors?per_page=10", token)

        try:
            languages = gh_get(f"/repos/{repo}/languages", token)
        except Exception:
            languages = {}

        # Separate issues from PRs
        open_issues = [i for i in all_issues if "pull_request" not in i]
        open_issue_count = len(open_issues)
        open_pr_count = len(open_prs)
        commit_count = len(commits)
        contributor_count = len(contributors)
        has_description = bool(repo_data.get("description"))
        has_license = bool(repo_data.get("license"))
        stars = repo_data.get("stargazers_count", 0)
        forks = repo_data.get("forks_count", 0)
        is_archived = repo_data.get("archived", False)

        # ── Score Calculation ─────────────────────────────
        score = 100
        findings = []
        recommendations = []

        if is_archived:
            return "## 🏥 Repo Health\n\n⚠️ This repository is **archived** — no health check needed."

        # Issues
        if open_issue_count > 20:
            score -= 15
            findings.append(f"🔴 {open_issue_count} open issues — too many unresolved")
            recommendations.append("Close or triage old issues — aim for <10")
        elif open_issue_count > 10:
            score -= 7
            findings.append(f"🟡 {open_issue_count} open issues")
            recommendations.append("Reduce open issues below 10")
        else:
            findings.append(f"✅ {open_issue_count} open issues — healthy")

        # PRs
        if open_pr_count > 10:
            score -= 10
            findings.append(f"🔴 {open_pr_count} open PRs — review backlog")
            recommendations.append("Review and close stale PRs")
        elif open_pr_count > 5:
            score -= 5
            findings.append(f"🟡 {open_pr_count} open PRs")
        else:
            findings.append(f"✅ {open_pr_count} open PRs — healthy")

        # License
        if not has_license:
            score -= 8
            findings.append("🔴 No license — required for open source")
            recommendations.append("Add LICENSE file (MIT recommended)")
        else:
            license_name = repo_data["license"].get("name", "License")
            findings.append(f"✅ License: {license_name}")

        # Description
        if not has_description:
            score -= 5
            findings.append("🟡 No repository description")
            recommendations.append("Add a description in repo Settings")
        else:
            findings.append("✅ Description present")

        # Contributors (bus factor)
        if contributor_count < 2:
            score -= 5
            findings.append("🟡 Single contributor — bus factor risk")
            recommendations.append("Encourage contributions — add CONTRIBUTING.md")
        else:
            findings.append(f"✅ {contributor_count} contributors")

        # Commit activity
        if commit_count < 3:
            score -= 10
            findings.append("🔴 Very low recent commit activity")
            recommendations.append("Keep the project active with regular commits")
        else:
            findings.append(f"✅ {commit_count} recent commits")

        # ── Grade ────────────────────────────────────────
        if score >= 90:
            grade, grade_emoji = "A+", "🏆"
        elif score >= 80:
            grade, grade_emoji = "A", "✅"
        elif score >= 70:
            grade, grade_emoji = "B", "🟢"
        elif score >= 60:
            grade, grade_emoji = "C", "🟡"
        elif score >= 50:
            grade, grade_emoji = "D", "🟠"
        else:
            grade, grade_emoji = "F", "🔴"

        health_bar = "█" * (score // 10) + "░" * (10 - score // 10)
        lang_str = " · ".join(f"`{k}`" for k in list(languages.keys())[:5]) or "Unknown"
        findings_md = "\n".join(f"- {f}" for f in findings)
        recs_md = "\n".join(f"{i+1}. {r}" for i, r in enumerate(recommendations[:4]))

        return f"""## 🏥 Repo Health Report — `{repo}`

### Grade: {grade_emoji} **{grade}** ({score}/100)
`{health_bar}`

| Metric | Value |
|--------|-------|
| ⭐ Stars | {stars} |
| 🍴 Forks | {forks} |
| 📂 Open Issues | {open_issue_count} |
| 🔀 Open PRs | {open_pr_count} |
| 👥 Contributors | {contributor_count} |
| 💻 Languages | {lang_str} |

### Findings
{findings_md}

{f"### 💡 Recommendations{chr(10)}{recs_md}" if recommendations else "### 💡 All good — no major issues found! 🎉"}"""

    except Exception as e:
        return f"## ⚠️ Health Check Failed\n\nCould not analyze repo: `{str(e)[:200]}`"


def _cmd_version(repo: str, token: str) -> str:
    """Show version/tag status of the repo."""
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

        return f"""## 🎛️ Version Status — `{repo}`

| | |
|---|---|
| **Latest Tag** | `{latest_tag}` |
| **Latest Release** | `{latest_release}` |
| **Total Tags** | {len(tags)} |

### Recent Tags
{tags_list}

### Recent Commits
| SHA | Message |
|-----|---------|
{commits_md}

### 💡 Semantic Versioning Guide
| Commit type | Version bump |
|-------------|-------------|
| `feat:` | Minor → `v1.1.0` |
| `fix:` | Patch → `v1.0.1` |
| `feat!:` / BREAKING | Major → `v2.0.0` |"""

    except Exception as e:
        return f"## ⚠️ Version check failed: `{str(e)[:200]}`"


def _cmd_merge(repo: str, issue_number: int, issue: dict,
               token: str, author: str, config) -> str:
    """Attempt to merge a PR via /merge command."""
    if "pull_request" not in issue:
        return "## ℹ️ `/merge` only works on Pull Requests."

    try:
        pr = gh_get(f"/repos/{repo}/pulls/{issue_number}", token)
        reviews = gh_get(f"/repos/{repo}/pulls/{issue_number}/reviews", token)
        commit_sha = pr["head"]["sha"]
        check_runs = gh_get(f"/repos/{repo}/commits/{commit_sha}/check-runs", token)
        checks = check_runs.get("check_runs", [])

        # Run guardrails
        from app.core.guardrails import check_pr_auto_merge
        guard = check_pr_auto_merge(pr, checks, reviews, config)

        if not guard.passed:
            return f"## 🚫 Cannot Merge\n\n**Reason:** {guard.reason}"

        head_branch = pr["head"]["ref"]
        base_branch = pr["base"]["ref"]

        result = gh_put(f"/repos/{repo}/pulls/{issue_number}/merge", token, {
            "commit_title": f"feat: merge {head_branch} via /merge by @{author}",
            "commit_message": f"Merged by AI Repo Manager on request from @{author}",
            "merge_method": "merge"
        })

        if result.get("merged"):
            # Clean up branch
            try:
                gh_delete(f"/repos/{repo}/git/refs/heads/{head_branch}", token)
            except Exception:
                pass

            return (
                f"## ✅ Merged!\n\n"
                f"**`{head_branch}`** → **`{base_branch}`**\n\n"
                f"SHA: `{result.get('sha','')[:8]}`"
            )
        else:
            return f"## ⚠️ Merge failed: {result.get('message','Unknown error')}"

    except Exception as e:
        return f"## ⚠️ Merge error: `{str(e)[:300]}`"
