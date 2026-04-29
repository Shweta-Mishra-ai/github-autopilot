"""
Comments Handler - app/handlers/comments.py
V3/V4: All slash commands.

FIXED (ruff F401 line 7):  Removed unused `import logging`.
FIXED (ruff F841 lines 351,352,356): Removed unused variable assignments in _cmd_health().
FIXED (ruff E702 lines 363,366,371,373,378,384): Split semicolons to separate lines.
"""

import re
from app.github.auth import get_installation_token
from app.github.client import gh_get, gh_post, gh_put, gh_delete, GitHubError
from app.ai.router import router
from app.ai.hallucination import check_response, add_confidence_footer
from app.core.config import load_config
from app.core.logger import EventLogger
from app.core.confidence import ConfidenceGate
from app.security.secrets import scan_diff, format_findings as format_secret_findings
from app.security.dependencies import scan_requirements_txt, format_dep_findings

SKIP_AUTHORS = {"dependabot[bot]", "renovate[bot]", "github-actions[bot]", "ai-repo-manager[bot]"}

ALL_COMMANDS = [
    "/impact", "/secfull", "/autofix", "/report", "/notify",
    "/fix", "/apply", "/explain", "/improve", "/test", "/docs",
    "/refactor", "/health", "/version", "/merge",
    "/summarize", "/ci", "/security", "/gaps", "/changelog",
    "/rollback", "/autofix", "/impact", "/perf", "/arch",
    "/release", "/runtests", "/secfull", "/budget",
]


def handle(payload: dict):
    action = payload.get("action")
    if action != "created":
        return

    comment  = payload["comment"]
    body     = comment.get("body", "")
    author   = comment["user"]["login"]
    repo     = payload["repository"]["full_name"]
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
    gate   = ConfidenceGate(config)

    if not config.command_enabled(cmd):
        try:
            gh_post(f"/repos/{repo}/issues/{issue_number}/comments", token, {
                "body": f"## ℹ️ Command Disabled\n\n`{cmd}` is disabled in `.ai-repo-manager.yml`.{config.footer}"
            })
        except Exception:
            pass
        return

    try:
        issue     = gh_get(f"/repos/{repo}/issues/{issue_number}", token)
        ctx_title = issue.get("title", "")
        ctx_body  = issue.get("body", "") or ""
    except Exception:
        ctx_title, ctx_body = "", ""

    code_match   = re.search(r'```[\w]*\n([\s\S]*?)\n```', body)
    code         = code_match.group(1) if code_match else ""
    context_text = re.sub(r'```[\s\S]*?```', '', body).replace(cmd, "").strip()
    full_context = code or context_text or ctx_body or ctx_title

    response = ""

    try:
        if cmd == "/fix":
            response = _cmd_fix(ctx_title, full_context, gate)
        elif cmd == "/apply":
            response = _cmd_apply(repo, issue_number, ctx_title, full_context, token)
        elif cmd == "/explain":
            response = _cmd_explain(full_context)
        elif cmd == "/improve":
            response = _cmd_improve(full_context, gate)
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
        elif cmd == "/summarize":
            response = _cmd_summarize(repo, issue_number, token)
        elif cmd == "/ci":
            response = _cmd_ci(full_context)
        elif cmd == "/security":
            response = _cmd_security(repo, issue_number, issue, token)
        elif cmd == "/gaps":
            response = _cmd_gaps(full_context)
        elif cmd == "/changelog":
            response = _cmd_changelog(repo, token)
        elif cmd == "/budget":
            response = _cmd_budget()
        elif cmd == "/rollback":
            response = _cmd_rollback(repo, issue_number, token, context_text, author)
        elif cmd == "/impact":
            response = _cmd_impact(repo, issue_number, issue, token)
        elif cmd == "/secfull":
            response = _cmd_secfull(repo, token)
        elif cmd == "/autofix":
            response = _cmd_autofix(repo, issue_number, issue, token, context_text)
        elif cmd == "/report":
            response = _cmd_report(repo)
        elif cmd == "/notify":
            response = _cmd_notify(repo, issue_number, issue, token, context_text)

    except Exception as e:
        log.error(f"Command {cmd} failed: {e}")
        response = f"## ⚠️ Command Error\n\n`{cmd}` failed: `{str(e)[:200]}`\n\nPlease try again."

    if response:
        full = f"{response}\n\n---\n*🤖 `{cmd}` — requested by @{author}*{config.footer}"
        try:
            gh_post(f"/repos/{repo}/issues/{issue_number}/comments", token, {"body": full})
            log.done(f"{cmd} response posted")
        except GitHubError as e:
            log.error(f"Could not post response: {e}")


# ── Command implementations ───────────────────────────────────────────────────

def _cmd_fix(ctx_title: str, context: str, gate=None) -> str:
    r, _meta = router.ask(
        "Senior engineer. Give precise, working fix. JSON only.",
        f"""Fix this issue:
Title: {ctx_title}
Context: {context[:2000]}

Return JSON:
{{
  "root_cause": "exact reason",
  "fix": "working code or commit fixes",
  "explanation": "why this fix works",
  "test": "test to verify fix",
  "confidence": 0.85
}}""",
        task="fix_command"
    )

    comment = (
        f"## 🔧 Fix\n\n"
        f"**Root cause:** {r.get('root_cause', 'See fix below')}\n\n"
        f"**Fix:**\n```\n{r.get('fix', '')}\n```\n\n"
        f"**Why:** {r.get('explanation', '')}\n\n"
        f"**Test:**\n```\n{r.get('test', '')}\n```"
    )
    hal = check_response(r, response_type="fix")
    return add_confidence_footer(comment, hal)


def _cmd_apply(repo: str, issue_number: int, ctx_title: str,
               context: str, token: str) -> str:
    try:
        repo_data      = gh_get(f"/repos/{repo}", token)
        default_branch = repo_data.get("default_branch", "main")
        commits        = gh_get(f"/repos/{repo}/commits?sha={default_branch}&per_page=20", token)

        if not commits:
            return "## ⚠️ No commits found."

        commit_list = "\n".join(
            f"- SHA: {c['sha']} | Message: {c['commit']['message'].split(chr(10))[0]}"
            for c in commits[:15]
        )

        r, _meta = router.ask(
            "Git expert. Identify non-conventional commits. JSON only.",
            f"""Issue: {ctx_title}
Recent commits:
{commit_list}

Return JSON:
{{
  "commits": [
    {{"sha": "full_sha", "old_message": "original", "new_message": "conventional: message"}}
  ]
}}""",
            task="commit_lint"
        )

        commits_to_fix = r.get("commits", [])
        if not commits_to_fix:
            return "## ✅ Nothing to Fix\n\nAll commits already follow Conventional Commits! 🎉"

        sha_map = {c['sha'][:7]: c['sha'] for c in commits}
        sha_map.update({c['sha']: c['sha'] for c in commits})

        # FIXED (BUG 3): Use branch name, create fix branch instead of force-pushing main
        import time
        fix_branch = f"autopilot/fix-commits-{int(time.time())}"
        ref_data   = gh_get(f"/repos/{repo}/git/ref/heads/{default_branch}", token)
        base_sha   = ref_data["object"]["sha"]

        # Create fix branch
        try:
            gh_post(f"/repos/{repo}/git/refs", token, {
                "ref": f"refs/heads/{fix_branch}",
                "sha": base_sha
            })
        except Exception as e:
            return f"## ⚠️ Could not create fix branch\n\n`{str(e)[:200]}`"

        last_sha = base_sha
        fixed, failed = [], []

        for c in commits_to_fix:
            sha     = c.get("sha", "").strip()
            new_msg = c.get("new_message", "").strip()
            old_msg = c.get("old_message", sha[:7])
            if not sha or not new_msg:
                continue

            full_sha = sha_map.get(sha, sha_map.get(sha[:7], sha))

            try:
                commit_data = gh_get(f"/repos/{repo}/git/commits/{full_sha}", token)
                new_commit  = gh_post(f"/repos/{repo}/git/commits", token, {
                    "message": new_msg,
                    "tree":    commit_data["tree"]["sha"],
                    "parents": [p["sha"] for p in commit_data.get("parents", [])]
                })
                last_sha = new_commit["sha"]
                fixed.append(f"✅ `{sha[:7]}` → `{new_msg}`\n   *(was: `{old_msg[:50]}`)*")
            except Exception as e:
                failed.append(f"❌ `{sha[:7]}` — {str(e)[:80]}")

        if fixed:
            try:
                from app.github.client import gh_patch
                gh_patch(f"/repos/{repo}/git/refs/heads/{fix_branch}", token, {
                    "sha": last_sha
                })
            except Exception as e:
                return f"## ⚠️ Commits created but branch update failed\n\n`{str(e)[:200]}`"

        if fixed:
            try:
                gh_post(f"/repos/{repo}/pulls", token, {
                    "title": f"fix: apply conventional commits (issue #{issue_number})",
                    "head":  fix_branch,
                    "base":  default_branch,
                    "body":  f"Fixes #{issue_number}\n\nAI-applied conventional commit fixes. Please review before merging."
                })
            except Exception:
                pass

        lines = ["## 🔧 Auto-Apply Results\n"]
        if fixed:
            lines.append(f"### ✅ Fixed ({len(fixed)} commits)\n")
            lines.extend(fixed)
        if failed:
            lines.append(f"\n### ❌ Failed ({len(failed)} commits)\n")
            lines.extend(failed)
        if fixed:
            lines.append(f"\n✨ Fix branch `{fix_branch}` created — PR opened for review!")
        return "\n".join(lines)

    except Exception as e:
        return f"## ⚠️ Apply Failed\n\n`{str(e)[:300]}`"


def _cmd_explain(context: str) -> str:
    text, _meta = router.ask_text(
        "Senior engineer. Explain clearly in plain English.",
        f"Explain this:\n{context[:2000]}",
        task="explain"
    )
    return f"## 💡 Explanation\n\n{text}"


def _cmd_improve(context: str, gate=None) -> str:
    r, _meta = router.ask(
        "Staff engineer. Suggest concrete improvements. JSON only.",
        f"""Suggest improvements for:
{context[:2000]}

Return JSON:
{{
  "summary": "overall assessment",
  "improvements": [
    {{"area": "performance|security|readability|structure", "suggestion": "what to change", "example": "code example"}}
  ]
}}""",
        task="improve"
    )
    lines = [f"## ✨ Improvements\n\n**{r.get('summary', '')}**\n"]
    for i, imp in enumerate(r.get("improvements", [])[:4], 1):
        lines.append(f"### {i}. `{imp.get('area','').upper()}` — {imp.get('suggestion','')}")
        if imp.get("example"):
            lines.append(f"```\n{imp['example'][:300]}\n```")
    return "\n\n".join(lines)


def _cmd_test(context: str) -> str:
    r, _meta = router.ask(
        "Senior QA engineer. Generate tests. JSON only.",
        f"""Write tests for:
{context[:2000]}

Return JSON:
{{
  "framework": "pytest",
  "tests": [{{"name": "test_name", "type": "unit", "desc": "what it tests", "code": "full test code"}}]
}}""",
        task="test_generation"
    )
    lines = [f"## 🧪 Tests ({r.get('framework', 'pytest')})\n"]
    for t in r.get("tests", [])[:3]:
        lines.append(f"### `{t.get('name','test')}` ({t.get('type','unit')})\n*{t.get('desc','')}*\n```python\n{t.get('code','')[:400]}\n```")
    return "\n\n".join(lines)


def _cmd_docs(context: str) -> str:
    r, _meta = router.ask(
        "Technical writer. Generate documentation. JSON only.",
        f"""Generate docs for:
{context[:2000]}

Return JSON:
{{"docstring": "complete docstring", "usage": "usage example", "readme_section": "markdown section"}}""",
        task="docs"
    )
    return (
        f"## 📚 Documentation\n\n"
        f"**Docstring:**\n```\n{r.get('docstring','')}\n```\n\n"
        f"**Usage:**\n```\n{r.get('usage','')}\n```\n\n"
        f"**README section:**\n{r.get('readme_section','')}"
    )


def _cmd_refactor(context: str) -> str:
    r, _meta = router.ask(
        "Principal engineer. Suggest refactoring. JSON only.",
        f"""Suggest refactoring for:
{context[:2500]}

Return JSON:
{{
  "summary": "assessment",
  "refactors": [{{"type": "extract_function", "description": "what and why", "before": "snippet", "after": "refactored", "benefit": "benefit"}}]
}}""",
        task="refactor"
    )
    lines = [f"## ♻️ Refactor\n\n**{r.get('summary','')}**\n"]
    for i, ref in enumerate(r.get("refactors", [])[:4], 1):
        lines.append(f"### {i}. `{ref.get('type','').upper()}` — {ref.get('description','')}")
        if ref.get("before"):
            lines.append(f"**Before:**\n```\n{ref['before'][:300]}\n```")
        if ref.get("after"):
            lines.append(f"**After:**\n```\n{ref['after'][:300]}\n```")
        lines.append(f"✅ **Benefit:** {ref.get('benefit','')}")
    return "\n\n".join(lines)


def _cmd_health(repo: str, token: str) -> str:
    try:
        repo_data  = gh_get(f"/repos/{repo}", token)
        all_issues = gh_get(f"/repos/{repo}/issues?state=open&per_page=50", token)
        open_prs   = gh_get(f"/repos/{repo}/pulls?state=open&per_page=20", token)

        # FIXED (F841): Removed unused variable assignments for commits, contributors, languages

        open_issues = [i for i in all_issues if "pull_request" not in i]
        score = 100
        findings, recommendations = [], []

        # FIXED (E702): Split semicolons to separate lines
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
            findings.append(f"✅ License: {repo_data['license'].get('name','')}")

        if not repo_data.get("description"):
            score -= 5
            findings.append("🟡 No description")
        else:
            findings.append("✅ Description present")

        grade = "A+" if score >= 90 else "A" if score >= 80 else "B" if score >= 70 else "C" if score >= 60 else "D" if score >= 50 else "F"
        bar   = "█" * (score // 10) + "░" * (10 - score // 10)

        return f"""## 🏥 Repo Health — `{repo}`

### Grade: **{grade}** ({score}/100)
`{bar}`

### Findings
{chr(10).join(f"- {f}" for f in findings)}

{f"### 💡 Recommendations{chr(10)}{chr(10).join(f'{i+1}. {r}' for i,r in enumerate(recommendations[:4]))}" if recommendations else "### 💡 All good!"}"""

    except Exception as e:
        return f"## ⚠️ Health Check Failed\n\n`{str(e)[:200]}`"


def _cmd_version(repo: str, token: str) -> str:
    try:
        tags     = gh_get(f"/repos/{repo}/tags?per_page=10", token)
        releases = gh_get(f"/repos/{repo}/releases?per_page=3", token)
        commits  = gh_get(f"/repos/{repo}/commits?per_page=8", token)

        latest_tag     = tags[0]["name"] if tags else "No tags yet"
        latest_release = releases[0]["name"] if releases else "No releases"
        tags_list      = "\n".join(f"- `{t['name']}`" for t in tags[:5]) or "- No tags yet"
        commits_md     = "\n".join(
            f"| `{c['sha'][:7]}` | {c['commit']['message'].split(chr(10))[0][:55]} |"
            for c in commits[:6]
        )

        return f"""## 🎛️ Version Status — `{repo}`

| | |
|---|---|
| **Latest Tag** | `{latest_tag}` |
| **Latest Release** | `{latest_release}` |

### Recent Tags
{tags_list}

### Recent Commits
| SHA | Message |
|-----|---------|
{commits_md}"""

    except Exception as e:
        return f"## ⚠️ Version check failed: `{str(e)[:200]}`"


def _cmd_merge(repo, issue_number, issue, token, author, config) -> str:
    if "pull_request" not in issue:
        return "## ℹ️ `/merge` only works on Pull Requests."
    try:
        pr        = gh_get(f"/repos/{repo}/pulls/{issue_number}", token)
        reviews   = gh_get(f"/repos/{repo}/pulls/{issue_number}/reviews", token)
        commit_sha = pr["head"]["sha"]
        check_runs = gh_get(f"/repos/{repo}/commits/{commit_sha}/check-runs", token)

        from app.core.guardrails import check_pr_auto_merge
        guard = check_pr_auto_merge(pr, check_runs.get("check_runs", []), reviews, config)
        if not guard.passed:
            return f"## 🚫 Cannot Merge\n\n**Reason:** {guard.reason}"

        head_branch = pr["head"]["ref"]
        base_branch = pr["base"]["ref"]
        result = gh_put(f"/repos/{repo}/pulls/{issue_number}/merge", token, {
            "commit_title": f"feat: merge {head_branch} via /merge by @{author}",
            "merge_method": "merge"
        })

        if result.get("merged"):
            try:
                gh_delete(f"/repos/{repo}/git/refs/heads/{head_branch}", token)
            except Exception:
                pass
            return f"## ✅ Merged!\n\n**`{head_branch}`** → **`{base_branch}`**\nSHA: `{result.get('sha','')[:8]}`"

        return f"## ⚠️ Merge failed: {result.get('message','Unknown error')}"

    except Exception as e:
        return f"## ⚠️ Merge error: `{str(e)[:300]}`"


def _cmd_summarize(repo: str, issue_number: int, token: str) -> str:
    try:
        comments = gh_get(f"/repos/{repo}/issues/{issue_number}/comments?per_page=50", token)
        thread   = "\n\n".join(
            f"@{c['user']['login']}: {c['body'][:300]}"
            for c in comments[:20]
        )
        summary, _meta = router.ask_text(
            "Senior engineer. Summarize GitHub discussions concisely.",
            f"Summarize this discussion thread:\n\n{thread[:3000]}",
            task="explain"
        )
        return f"## 📝 Thread Summary\n\n{summary}"
    except Exception as e:
        return f"## ⚠️ Summarize failed: `{str(e)[:200]}`"


def _cmd_ci(context: str) -> str:
    r, _meta = router.ask(
        "DevOps expert. Analyze CI failures. JSON only.",
        f"""Analyze this CI failure:
{context[:3000]}

Return JSON:
{{
  "root_cause": "exact reason for failure",
  "fix": "exact steps to fix",
  "prevention": "how to prevent this in future",
  "confidence": 0.85
}}""",
        task="ci_analysis"
    )
    return (
        f"## 🔴 CI Failure Analysis\n\n"
        f"**Root Cause:** {r.get('root_cause', 'See below')}\n\n"
        f"**Fix:**\n```\n{r.get('fix', '')}\n```\n\n"
        f"**Prevention:** {r.get('prevention', '')}"
    )


def _cmd_security(repo: str, issue_number: int, issue: dict, token: str) -> str:
    if "pull_request" not in issue:
        return "## ℹ️ `/security` works best on Pull Requests."
    try:
        pr_files     = gh_get(f"/repos/{repo}/pulls/{issue_number}/files", token)
        all_findings = []

        for f in pr_files[:10]:
            patch = f.get("patch", "")
            if patch:
                all_findings.extend(scan_diff(patch))

        req_files   = [f for f in pr_files if f["filename"] in ("requirements.txt",)]
        dep_findings = []
        for f in req_files:
            raw     = gh_get(f"/repos/{repo}/contents/{f['filename']}", token)
            import base64
            content = base64.b64decode(raw["content"]).decode()
            dep_findings.extend(scan_requirements_txt(content))

        lines = ["## 🔒 Security Scan Results\n"]
        if all_findings:
            lines.append(format_secret_findings(all_findings, repo))
        else:
            lines.append("✅ **No secrets detected** in changed files.\n")

        if dep_findings:
            lines.append(format_dep_findings(dep_findings))
        else:
            lines.append("✅ **No vulnerable dependencies** found.\n")

        return "\n\n".join(lines)

    except Exception as e:
        return f"## ⚠️ Security scan failed: `{str(e)[:200]}`"


def _cmd_gaps(context: str) -> str:
    r, _meta = router.ask(
        "Senior QA engineer. Identify test gaps. JSON only.",
        f"""Analyze this code for test coverage gaps:
{context[:2500]}

Return JSON:
{{
  "coverage_assessment": "overall assessment",
  "gaps": [
    {{"area": "what is not tested", "risk": "high|medium|low", "suggested_test": "test to add"}}
  ]
}}""",
        task="gaps"
    )
    lines = [f"## 🔍 Test Coverage Gaps\n\n**{r.get('coverage_assessment', '')}**\n"]
    for i, gap in enumerate(r.get("gaps", [])[:5], 1):
        lines.append(
            f"### {i}. {gap.get('area', '')} — Risk: `{gap.get('risk', 'medium').upper()}`\n"
            f"**Suggested test:** {gap.get('suggested_test', '')}"
        )
    return "\n\n".join(lines)


def _cmd_changelog(repo: str, token: str) -> str:
    try:
        commits    = gh_get(f"/repos/{repo}/commits?per_page=20", token)
        tags       = gh_get(f"/repos/{repo}/tags?per_page=1", token)
        latest_tag = tags[0]["name"] if tags else "v0.0.0"

        commit_list = "\n".join(
            f"- {c['commit']['message'].split(chr(10))[0]}"
            for c in commits[:15]
        )

        changelog, _meta = router.ask_text(
            "Technical writer. Generate a clean CHANGELOG entry in Keep a Changelog format.",
            f"""Generate a CHANGELOG.md entry for version after {latest_tag}.

Recent commits:
{commit_list}

Format:
## [X.Y.Z] - YYYY-MM-DD
### Added
### Changed
### Fixed""",
            task="changelog"
        )

        return f"## 📋 CHANGELOG Entry\n\n```markdown\n{changelog}\n```"

    except Exception as e:
        return f"## ⚠️ Changelog generation failed: `{str(e)[:200]}`"


def _cmd_budget() -> str:
    """Show today's LLM usage per provider — powered by metrics module."""
    try:
        from app.ai.metrics import format_budget_comment
        return format_budget_comment()
    except Exception as e:
        return f"## ⚠️ Budget check failed: `{str(e)[:200]}`"

def _cmd_rollback(repo: str, issue_number: int, token: str,
                  cmd_args: str, author: str) -> str:
    """
    /rollback        → show available snapshots
    /rollback 2      → restore snapshot #2
    """
    from app.core.snapshot import (
        get_snapshot_by_number,
        format_snapshot_list, format_rollback_result, take_snapshot,
    )

    # No args → list snapshots
    if not cmd_args:
        return format_snapshot_list(repo)

    # With number → restore
    try:
        n = int(cmd_args.strip())
    except ValueError:
        return (
            f"## ⚠️ Invalid Snapshot Number\n\n"
            f"`{cmd_args}` is not a valid number.\n\n"
            "Use `/rollback` to see available snapshots."
        )

    snap = get_snapshot_by_number(repo, n)
    if not snap:
        return (
            f"## ⚠️ Snapshot #{n} Not Found\n\n"
            "Use `/rollback` to see available snapshots (max 10, expire after 7 days)."
        )

    # Safety snapshot before restoring
    take_snapshot(repo, token, trigger=f"pre_rollback_by_{author}")

    restored = []
    failed   = []
    bot_actions = snap.get("bot_actions", [])

    for action in reversed(bot_actions):
        action_type = action.get("type", "")
        try:
            if action_type == "create_issue":
                number = action.get("number")
                if number:
                    gh_put(f"/repos/{repo}/issues/{number}", token, {"state": "closed"})
                    restored.append(f"Closed issue #{number}: {action.get('title','')[:50]}")

            elif action_type == "edit_pr_title":
                number    = action.get("number")
                old_title = action.get("old_title", "")
                if number and old_title:
                    gh_put(f"/repos/{repo}/pulls/{number}", token, {"title": old_title})
                    restored.append(f"Reverted PR #{number} title to: {old_title[:50]}")

            elif action_type == "add_labels":
                number = action.get("number")
                labels = action.get("labels", [])
                if number and labels:
                    for lbl in labels:
                        try:
                            from app.github.client import gh_delete
                            gh_delete(f"/repos/{repo}/issues/{number}/labels/{lbl}", token)
                        except Exception:
                            pass
                    restored.append(f"Removed labels {labels} from #{number}")

        except Exception as exc:
            failed.append(f"{action_type} #{action.get('number','?')}: {str(exc)[:60]}")

    if not bot_actions:
        restored.append("No automated actions to undo in this snapshot")

    return format_rollback_result(repo, snap, restored, failed)

def _cmd_impact(repo: str, issue_number: int, issue: dict, token: str) -> str:
    """
    /impact — Show blast radius: which layers/modules this PR affects.
    Works on Pull Requests only.
    """
    if "pull_request" not in issue:
        return "## ℹ️ `/impact` only works on Pull Requests."

    try:
        from app.github.client import gh_get
        from app.handlers.pull_request import _blast_radius
        from app.ai.router import router

        files = gh_get(f"/repos/{repo}/pulls/{issue_number}/files", token)
        blast = _blast_radius(files)

        filenames = [f["filename"] for f in files[:15]]
        r, _meta = router.ask(
            "Senior architect. Analyze PR impact on system. JSON only.",
            f"""Analyze the blast radius of these file changes:
{chr(10).join(filenames)}

Return JSON:
{{
  "summary": "one sentence overall impact",
  "affected_systems": ["system1", "system2"],
  "breaking_change_risk": "low|medium|high",
  "requires_migration": false,
  "review_priority": "low|medium|high",
  "notes": "any important considerations"
}}""",
            task="arch",
        )

        bc_risk  = r.get("breaking_change_risk", "low")
        bc_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(bc_risk, "🟡")
        migration = "⚠️ Yes" if r.get("requires_migration") else "✅ No"
        systems   = ", ".join(f"`{s}`" for s in r.get("affected_systems", [])[:5])

        return f"""## 💥 Blast Radius — PR #{issue_number}

**Summary:** {r.get("summary", "")}

### Layers Affected
{blast}

### Impact Assessment
| | |
|---|---|
| **Breaking Change Risk** | {bc_emoji} {bc_risk.capitalize()} |
| **Requires Migration** | {migration} |
| **Review Priority** | `{r.get("review_priority", "medium")}` |
| **Affected Systems** | {systems or "none identified"} |

{f"> ℹ️ {r.get('notes', '')}" if r.get("notes") else ""}"""

    except Exception as e:
        return f"## ⚠️ Impact analysis failed: `{str(e)[:200]}`"


def _cmd_secfull(repo: str, token: str) -> str:
    """
    /secfull — Full security report using all 3 GitHub Security APIs:
    Dependabot Alerts + CodeQL + Secret Scanning.
    """
    try:
        from app.security.scanner import run_security_scan
        report = run_security_scan(repo, token)
        return report.to_markdown(include_low=True)
    except Exception as e:
        return f"## ⚠️ Security scan failed: `{str(e)[:200]}`"

def _cmd_autofix(
    repo: str,
    issue_number: int,
    issue: dict,
    token: str,
    cmd_args: str,
) -> str:
    """
    /autofix              → AI picks file and creates fix PR
    /autofix app/auth.py  → Fix specific file
    """
    from app.handlers.autofix import run_autofix
    target_file = cmd_args.strip() if cmd_args else ""
    return run_autofix(repo, issue_number, issue, token, target_file)


def _cmd_report(repo: str) -> str:
    """
    /report → Post weekly analytics report for this repo.
    Shows: PR velocity, issue resolution, code quality grade, bot usage.
    """
    try:
        from app.core.analytics import format_report_comment, record_command_used
        record_command_used(repo, "report")
        return format_report_comment(repo)
    except Exception as e:
        return f"## ⚠️ Report failed: `{str(e)[:200]}`"

def _cmd_notify(
    repo: str,
    issue_number: int,
    issue: dict,
    token: str,
    cmd_args: str,
) -> str:
    """/notify — Send Discord alert for this issue/PR."""
    try:
        from app.github.notifications import send_rich_discord
        title  = issue.get("title", f"Issue #{issue_number}")
        is_pr  = "pull_request" in issue
        labels = [lb.get("name", "") for lb in issue.get("labels", [])]
        kind   = "PR" if is_pr else "Issue"
        url    = issue.get("html_url", f"https://github.com/{repo}/issues/{issue_number}")
        color  = 0x5865F2
        if any("bug" in lb.lower() for lb in labels):
            color = 0xE74C3C
        elif any("security" in lb.lower() for lb in labels):
            color = 0xE74C3C
        elif any("feature" in lb.lower() for lb in labels):
            color = 0x2ECC71
        desc = (
            "**Repo:** `" + repo + "`\n"
            "**Labels:** " + (", ".join(labels) or "none")
        )
        success, msg = send_rich_discord(
            title=f"\U0001f514 {kind} #{issue_number} \u2014 {title[:80]}",
            description=desc,
            color=color,
            fields=[
                {"name": "Type", "value": kind, "inline": True},
                {"name": "Number", "value": f"#{issue_number}", "inline": True},
            ],
            url=url,
        )
        if success:
            return f"## \U0001f514 Notification Sent!\n\nDiscord alert posted for {kind} #{issue_number}."
        return f"## \u26a0\ufe0f Notification Failed\n\n`{msg}`\n\nCheck DISCORD_WEBHOOK_URL in Render env."
    except Exception as e:
        return f"## \u26a0\ufe0f Notify error: `{str(e)[:200]}`"
