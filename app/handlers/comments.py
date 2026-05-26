"""
Comments Handler - app/handlers/comments.py
V4.1 — Security hardened.

CHANGES vs V4:
  - ALL_COMMANDS deduplicated (was 31 entries with 5 dupes → 26 unique, sorted)
  - check_command_permission() wired in before every restricted command
  - Per-user rate limit: 10 commands/hour/repo via Redis
  - Both checks post explanatory GitHub comments on denial (not silent drop)

ORIGINAL BUGS FIXED (carried from V3/V4):
  ruff F401 line 7:  Removed unused `import logging`
  ruff F841 lines 351,352,356: Removed unused vars in _cmd_health()
  ruff E702 lines 363,366,371,373,378,384: Split semicolons to separate lines
"""

import re
import time as _time

from app.core.authorization import check_command_permission
from app.core.config import load_config
from app.core.confidence import ConfidenceGate
from app.core.logger import EventLogger
from app.ai.hallucination import add_confidence_footer, check_response
from app.ai.router import router
from app.github.auth import get_installation_token
from app.github.client import GitHubError, gh_delete, gh_get, gh_post, gh_put
from app.security.enhanced_secrets import (
    format_findings as format_secret_findings,
    scan_diff,
)
from app.security.dependencies import scan_requirements_txt, format_dep_findings

SKIP_AUTHORS = {
    "dependabot[bot]",
    "renovate[bot]",
    "github-actions[bot]",
    "ai-repo-manager[bot]",
}

# Deduplicated, sorted — was 31 entries with 5 duplicates
ALL_COMMANDS = sorted({
    "/apply", "/arch", "/autofix", "/budget", "/changelog",
    "/ci", "/docs", "/explain", "/fix", "/gaps",
    "/health", "/impact", "/improve", "/merge", "/notify",
    "/perf", "/refactor", "/release", "/report", "/rollback",
    "/runtests", "/secfull", "/security", "/summarize", "/test",
    "/version",
})

# ── Per-user rate limiting ────────────────────────────────────────────────────

_USER_CMD_LIMIT  = 10    # commands per user per hour
_USER_CMD_WINDOW = 3600  # seconds


def _check_user_rate_limit(repo: str, author: str) -> bool:
    """
    Returns True if user is within limit (10 commands/hour/repo).
    Fail-open when Redis is unavailable so the bot stays usable.
    """
    try:
        from app.core.redis_client import get_redis
        r   = get_redis()
        key = f"cmd_rl:{repo}:{author}:{int(_time.time() // _USER_CMD_WINDOW)}"
        cnt = r.incr(key)
        r.expire(key, _USER_CMD_WINDOW)
        return int(cnt) <= _USER_CMD_LIMIT
    except Exception:
        return True  # Redis unavailable → allow


# ── Main handler ──────────────────────────────────────────────────────────────

def handle(payload: dict):
    action = payload.get("action")
    if action != "created":
        return

    comment      = payload["comment"]
    body         = comment.get("body", "")
    author       = comment["user"]["login"]
    repo         = payload["repository"]["full_name"]
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

    # ── Command enabled check ─────────────────────────────────────────────
    if not config.command_enabled(cmd):
        try:
            gh_post(f"/repos/{repo}/issues/{issue_number}/comments", token, {
                "body": (
                    f"## ℹ️ Command Disabled\n\n"
                    f"`{cmd}` is disabled in `.ai-repo-manager.yml`."
                    f"{config.footer}"
                )
            })
        except Exception:
            pass
        return

    # ── Per-user rate limit ───────────────────────────────────────────────
    if not _check_user_rate_limit(repo, author):
        try:
            gh_post(f"/repos/{repo}/issues/{issue_number}/comments", token, {
                "body": (
                    f"## ⏱️ Rate Limit\n\n"
                    f"@{author} you've used **{_USER_CMD_LIMIT} commands** "
                    f"in the last hour on this repo. "
                    f"Please wait before trying again.\n\n"
                    f"*Limit resets hourly to prevent API abuse.*"
                    f"{config.footer}"
                )
            })
        except Exception:
            pass
        log.warn(f"user_rate_limit hit for @{author}")
        return

    # ── Permission check for restricted commands ──────────────────────────
    allowed, denial_reason = check_command_permission(
        cmd, repo, author, token, config
    )
    if not allowed:
        try:
            gh_post(f"/repos/{repo}/issues/{issue_number}/comments", token, {
                "body": (
                    f"## ⛔ Permission Denied\n\n"
                    f"@{author}: {denial_reason}"
                    f"{config.footer}"
                )
            })
        except Exception:
            pass
        log.warn(f"permission_denied cmd={cmd} user={author}")
        return

    # ── Fetch issue context ───────────────────────────────────────────────
    try:
        issue     = gh_get(f"/repos/{repo}/issues/{issue_number}", token)
        ctx_title = issue.get("title", "")
        ctx_body  = issue.get("body", "") or ""
    except Exception:
        issue, ctx_title, ctx_body = {}, "", ""

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
        elif cmd == "/perf":
            response = _cmd_perf(full_context)
        elif cmd == "/arch":
            response = _cmd_arch(repo, issue_number, issue, token)
        elif cmd == "/release":
            response = _cmd_release(repo, token, author)
        elif cmd == "/runtests":
            response = _cmd_runtests(repo, issue_number, token)

    except Exception as e:
        log.error(f"Command {cmd} failed: {e}")
        response = (
            f"## ⚠️ Command Error\n\n"
            f"`{cmd}` failed: `{str(e)[:200]}`\n\nPlease try again."
        )

    if response:
        full = (
            f"{response}\n\n---\n"
            f"*🤖 `{cmd}` — requested by @{author}*{config.footer}"
        )
        try:
            gh_post(
                f"/repos/{repo}/issues/{issue_number}/comments",
                token,
                {"body": full},
            )
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


def _cmd_apply(
    repo: str, issue_number: int, ctx_title: str,
    context: str, token: str
) -> str:
    try:
        repo_data      = gh_get(f"/repos/{repo}", token)
        default_branch = repo_data.get("default_branch", "main")
        commits        = gh_get(
            f"/repos/{repo}/commits?sha={default_branch}&per_page=20", token
        )

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
            return (
                "## ✅ Nothing to Fix\n\n"
                "All commits already follow Conventional Commits! 🎉"
            )

        sha_map = {c["sha"][:7]: c["sha"] for c in commits}
        sha_map.update({c["sha"]: c["sha"] for c in commits})

        fix_branch = f"autopilot/fix-commits-{int(_time.time())}"
        ref_data   = gh_get(
            f"/repos/{repo}/git/ref/heads/{default_branch}", token
        )
        base_sha   = ref_data["object"]["sha"]

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
                fixed.append(
                    f"✅ `{sha[:7]}` → `{new_msg}`\n   *(was: `{old_msg[:50]}`)*"
                )
            except Exception as e:
                failed.append(f"❌ `{sha[:7]}` — {str(e)[:80]}")

        if fixed:
            try:
                from app.github.client import gh_patch
                gh_patch(f"/repos/{repo}/git/refs/heads/{fix_branch}", token, {
                    "sha": last_sha
                })
            except Exception as e:
                return (
                    f"## ⚠️ Commits created but branch update failed\n\n"
                    f"`{str(e)[:200]}`"
                )

        if fixed:
            try:
                gh_post(f"/repos/{repo}/pulls", token, {
                    "title": f"fix: apply conventional commits (issue #{issue_number})",
                    "head":  fix_branch,
                    "base":  default_branch,
                    "body": (
                        f"Fixes #{issue_number}\n\n"
                        "AI-applied conventional commit fixes. "
                        "Please review before merging."
                    )
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
            lines.append(
                f"\n✨ Fix branch `{fix_branch}` created — PR opened for review!"
            )
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
    {{"area": "performance|security|readability|structure",
      "suggestion": "what to change",
      "example": "code example"}}
  ]
}}""",
        task="improve"
    )
    lines = [f"## ✨ Improvements\n\n**{r.get('summary', '')}**\n"]
    for i, imp in enumerate(r.get("improvements", [])[:4], 1):
        lines.append(
            f"### {i}. `{imp.get('area','').upper()}` "
            f"— {imp.get('suggestion','')}"
        )
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
  "tests": [
    {{"name": "test_name", "type": "unit",
      "desc": "what it tests", "code": "full test code"}}
  ]
}}""",
        task="test_generation"
    )
    lines = [f"## 🧪 Tests ({r.get('framework', 'pytest')})\n"]
    for t in r.get("tests", [])[:3]:
        lines.append(
            f"### `{t.get('name','test')}` ({t.get('type','unit')})\n"
            f"*{t.get('desc','')}*\n"
            f"```python\n{t.get('code','')[:400]}\n```"
        )
    return "\n\n".join(lines)


def _cmd_docs(context: str) -> str:
    r, _meta = router.ask(
        "Technical writer. Generate documentation. JSON only.",
        f"""Generate docs for:
{context[:2000]}

Return JSON:
{{
  "docstring": "complete docstring",
  "usage": "usage example",
  "readme_section": "markdown section"
}}""",
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
  "refactors": [
    {{"type": "extract_function",
      "description": "what and why",
      "before": "snippet",
      "after": "refactored",
      "benefit": "benefit"}}
  ]
}}""",
        task="refactor"
    )
    lines = [f"## ♻️ Refactor\n\n**{r.get('summary','')}**\n"]
    for i, ref in enumerate(r.get("refactors", [])[:4], 1):
        lines.append(
            f"### {i}. `{ref.get('type','').upper()}` "
            f"— {ref.get('description','')}"
        )
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

        open_issues = [i for i in all_issues if "pull_request" not in i]
        score = 100
        findings, recommendations = [], []

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
            findings.append(
                f"✅ License: {repo_data['license'].get('name','')}"
            )

        if not repo_data.get("description"):
            score -= 5
            findings.append("🟡 No description")
        else:
            findings.append("✅ Description present")

        grade = (
            "A+" if score >= 90
            else "A" if score >= 80
            else "B" if score >= 70
            else "C" if score >= 60
            else "D" if score >= 50
            else "F"
        )
        bar = "█" * (score // 10) + "░" * (10 - score // 10)

        rec_section = ""
        if recommendations:
            rec_lines = "\n".join(
                f"{i+1}. {r}" for i, r in enumerate(recommendations[:4])
            )
            rec_section = f"\n### 💡 Recommendations\n{rec_lines}"
        else:
            rec_section = "\n### 💡 All good!"

        findings_md = "\n".join(f"- {f}" for f in findings)

        return (
            f"## 🏥 Repo Health — `{repo}`\n\n"
            f"### Grade: **{grade}** ({score}/100)\n"
            f"`{bar}`\n\n"
            f"### Findings\n{findings_md}"
            f"{rec_section}"
        )

    except Exception as e:
        return f"## ⚠️ Health Check Failed\n\n`{str(e)[:200]}`"


def _cmd_version(repo: str, token: str) -> str:
    try:
        tags     = gh_get(f"/repos/{repo}/tags?per_page=10", token)
        releases = gh_get(f"/repos/{repo}/releases?per_page=3", token)
        commits  = gh_get(f"/repos/{repo}/commits?per_page=8", token)

        latest_tag     = tags[0]["name"] if tags else "No tags yet"
        latest_release = releases[0]["name"] if releases else "No releases"
        tags_list      = (
            "\n".join(f"- `{t['name']}`" for t in tags[:5])
            or "- No tags yet"
        )
        commits_md = "\n".join(
            f"| `{c['sha'][:7]}` | "
            f"{c['commit']['message'].split(chr(10))[0][:55]} |"
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

    except Exception as e:
        return f"## ⚠️ Version check failed: `{str(e)[:200]}`"


def _cmd_merge(
    repo: str, issue_number: int, issue: dict,
    token: str, author: str, config
) -> str:
    if "pull_request" not in issue:
        return "## ℹ️ `/merge` only works on Pull Requests."
    try:
        pr         = gh_get(f"/repos/{repo}/pulls/{issue_number}", token)
        reviews    = gh_get(
            f"/repos/{repo}/pulls/{issue_number}/reviews", token
        )
        commit_sha = pr["head"]["sha"]
        check_runs = gh_get(
            f"/repos/{repo}/commits/{commit_sha}/check-runs", token
        )

        from app.core.guardrails import check_pr_auto_merge
        guard = check_pr_auto_merge(
            pr, check_runs.get("check_runs", []), reviews, config
        )
        if not guard.passed:
            return f"## 🚫 Cannot Merge\n\n**Reason:** {guard.reason}"

        head_branch = pr["head"]["ref"]
        base_branch = pr["base"]["ref"]
        result = gh_put(f"/repos/{repo}/pulls/{issue_number}/merge", token, {
            "commit_title": (
                f"feat: merge {head_branch} via /merge by @{author}"
            ),
            "merge_method": "merge"
        })

        if result.get("merged"):
            try:
                gh_delete(
                    f"/repos/{repo}/git/refs/heads/{head_branch}", token
                )
            except Exception:
                pass
            return (
                f"## ✅ Merged!\n\n"
                f"**`{head_branch}`** → **`{base_branch}`**\n"
                f"SHA: `{result.get('sha','')[:8]}`"
            )

        return f"## ⚠️ Merge failed: {result.get('message','Unknown error')}"

    except Exception as e:
        return f"## ⚠️ Merge error: `{str(e)[:300]}`"


def _cmd_summarize(repo: str, issue_number: int, token: str) -> str:
    try:
        comments = gh_get(
            f"/repos/{repo}/issues/{issue_number}/comments?per_page=50",
            token,
        )
        thread = "\n\n".join(
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


def _cmd_security(
    repo: str, issue_number: int, issue: dict, token: str
) -> str:
    if "pull_request" not in issue:
        return "## ℹ️ `/security` works best on Pull Requests."
    try:
        pr_files     = gh_get(f"/repos/{repo}/pulls/{issue_number}/files", token)
        all_findings = []

        for f in pr_files[:10]:
            patch = f.get("patch", "")
            if patch:
                filename = f.get("filename", "")
                all_findings.extend(scan_diff(patch, file_path=filename))

        req_files    = [f for f in pr_files if f["filename"] == "requirements.txt"]
        dep_findings = []
        for f in req_files:
            import base64
            raw     = gh_get(f"/repos/{repo}/contents/{f['filename']}", token)
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
    {{"area": "what is not tested",
      "risk": "high|medium|low",
      "suggested_test": "test to add"}}
  ]
}}""",
        task="gaps"
    )
    lines = [
        f"## 🔍 Test Coverage Gaps\n\n"
        f"**{r.get('coverage_assessment', '')}**\n"
    ]
    for i, gap in enumerate(r.get("gaps", [])[:5], 1):
        lines.append(
            f"### {i}. {gap.get('area', '')} "
            f"— Risk: `{gap.get('risk', 'medium').upper()}`\n"
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
            "Technical writer. Generate a clean CHANGELOG entry in "
            "Keep a Changelog format.",
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
    try:
        from app.ai.metrics import format_budget_comment
        return format_budget_comment()
    except Exception as e:
        return f"## ⚠️ Budget check failed: `{str(e)[:200]}`"


def _cmd_rollback(
    repo: str, issue_number: int, token: str,
    cmd_args: str, author: str
) -> str:
    from app.core.snapshot import (
        get_snapshot_by_number,
        format_snapshot_list,
        format_rollback_result,
        take_snapshot,
    )

    if not cmd_args:
        return format_snapshot_list(repo)

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
            "Use `/rollback` to see available snapshots "
            "(max 10, expire after 7 days)."
        )

    # Safety snapshot before restoring
    take_snapshot(repo, token, trigger=f"pre_rollback_by_{author}")

    restored    = []
    failed      = []
    bot_actions = snap.get("bot_actions", [])

    for action in reversed(bot_actions):
        action_type = action.get("type", "")
        try:
            if action_type == "create_issue":
                number = action.get("number")
                if number:
                    gh_put(
                        f"/repos/{repo}/issues/{number}",
                        token,
                        {"state": "closed"},
                    )
                    restored.append(
                        f"Closed issue #{number}: "
                        f"{action.get('title','')[:50]}"
                    )

            elif action_type == "edit_pr_title":
                number    = action.get("number")
                old_title = action.get("old_title", "")
                if number and old_title:
                    gh_put(
                        f"/repos/{repo}/pulls/{number}",
                        token,
                        {"title": old_title},
                    )
                    restored.append(
                        f"Reverted PR #{number} title to: {old_title[:50]}"
                    )

            elif action_type == "add_labels":
                number = action.get("number")
                labels = action.get("labels", [])
                if number and labels:
                    for lbl in labels:
                        try:
                            gh_delete(
                                f"/repos/{repo}/issues/{number}/labels/{lbl}",
                                token,
                            )
                        except Exception:
                            pass
                    restored.append(
                        f"Removed labels {labels} from #{number}"
                    )

        except Exception as exc:
            failed.append(
                f"{action_type} #{action.get('number','?')}: "
                f"{str(exc)[:60]}"
            )

    if not bot_actions:
        restored.append("No automated actions to undo in this snapshot")

    return format_rollback_result(repo, snap, restored, failed)


def _cmd_impact(
    repo: str, issue_number: int, issue: dict, token: str
) -> str:
    if "pull_request" not in issue:
        return "## ℹ️ `/impact` only works on Pull Requests."

    try:
        from app.handlers.pull_request import _blast_radius

        files  = gh_get(f"/repos/{repo}/pulls/{issue_number}/files", token)
        blast  = _blast_radius(files)

        filenames = [f["filename"] for f in files[:15]]
        r, _meta  = router.ask(
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
        systems   = ", ".join(
            f"`{s}`" for s in r.get("affected_systems", [])[:5]
        )

        notes_section = (
            f"\n> ℹ️ {r.get('notes', '')}" if r.get("notes") else ""
        )

        return (
            f"## 💥 Blast Radius — PR #{issue_number}\n\n"
            f"**Summary:** {r.get('summary', '')}\n\n"
            f"### Layers Affected\n{blast}\n\n"
            f"### Impact Assessment\n| | |\n|---|---|\n"
            f"| **Breaking Change Risk** | {bc_emoji} {bc_risk.capitalize()} |\n"
            f"| **Requires Migration** | {migration} |\n"
            f"| **Review Priority** | `{r.get('review_priority', 'medium')}` |\n"
            f"| **Affected Systems** | {systems or 'none identified'} |"
            f"{notes_section}"
        )

    except Exception as e:
        return f"## ⚠️ Impact analysis failed: `{str(e)[:200]}`"


def _cmd_secfull(repo: str, token: str) -> str:
    try:
        from app.security.scanner import run_security_scan
        report = run_security_scan(repo, token)
        return report.to_markdown(include_low=True)
    except Exception as e:
        return f"## ⚠️ Security scan failed: `{str(e)[:200]}`"


def _cmd_autofix(
    repo: str, issue_number: int, issue: dict,
    token: str, cmd_args: str
) -> str:
    from app.handlers.autofix import run_autofix
    target_file = cmd_args.strip() if cmd_args else ""
    return run_autofix(repo, issue_number, issue, token, target_file)


def _cmd_report(repo: str) -> str:
    try:
        from app.core.analytics import format_report_comment, record_command_used
        record_command_used(repo, "report")
        return format_report_comment(repo)
    except Exception as e:
        return f"## ⚠️ Report failed: `{str(e)[:200]}`"


def _cmd_notify(
    repo: str, issue_number: int, issue: dict,
    token: str, cmd_args: str
) -> str:
    try:
        from app.github.notifications import send_rich_discord
        title  = issue.get("title", f"Issue #{issue_number}")
        is_pr  = "pull_request" in issue
        labels = [lb.get("name", "") for lb in issue.get("labels", [])]
        kind   = "PR" if is_pr else "Issue"
        url    = issue.get(
            "html_url",
            f"https://github.com/{repo}/issues/{issue_number}",
        )
        color = 0x5865F2
        if any("bug" in lb.lower() for lb in labels):
            color = 0xE74C3C
        elif any("security" in lb.lower() for lb in labels):
            color = 0xE74C3C
        elif any("feature" in lb.lower() for lb in labels):
            color = 0x2ECC71

        desc = (
            f"**Repo:** `{repo}`\n"
            f"**Labels:** {', '.join(labels) or 'none'}"
        )
        success, msg = send_rich_discord(
            title=f"🔔 {kind} #{issue_number} — {title[:80]}",
            description=desc,
            color=color,
            fields=[
                {"name": "Type",   "value": kind,            "inline": True},
                {"name": "Number", "value": f"#{issue_number}", "inline": True},
            ],
            url=url,
        )
        if success:
            return (
                f"## 🔔 Notification Sent!\n\n"
                f"Discord alert posted for {kind} #{issue_number}."
            )
        return (
            f"## ⚠️ Notification Failed\n\n"
            f"`{msg}`\n\nCheck DISCORD_WEBHOOK_URL in Render env."
        )
    except Exception as e:
        return f"## ⚠️ Notify error: `{str(e)[:200]}`"


def _cmd_perf(context: str) -> str:
    r, _meta = router.ask(
        "You are a performance engineer. Analyze code for performance "
        "issues. JSON only.",
        f"""Analyze this code for performance problems:

{context[:2500]}

Look for:
- Time complexity (O(n²), O(n³), nested loops)
- Memory leaks or excessive allocations
- N+1 database/API query patterns
- Blocking I/O in async context
- Unnecessary recomputation (missing caching)
- Large objects in memory

Return JSON:
{{
  "overall_rating": "fast|acceptable|slow|critical",
  "complexity_issues": [
    {{
      "location": "function or line",
      "current_complexity": "O(n²)",
      "issue": "what is slow",
      "fix": "optimized version",
      "improvement": "estimated speedup"
    }}
  ],
  "quick_wins": ["easy optimization 1", "easy optimization 2"],
  "summary": "2 sentence overall assessment"
}}""",
        task="perf",
        max_tokens=1500,
    )

    rating  = r.get("overall_rating", "acceptable")
    r_emoji = {
        "fast":       "🟢",
        "acceptable": "🟡",
        "slow":       "🟠",
        "critical":   "🔴",
    }.get(rating, "🟡")

    issues_md = ""
    for i, issue in enumerate(r.get("complexity_issues", [])[:4], 1):
        issues_md += (
            f"\n### {i}. `{issue.get('location', '')}` "
            f"— {issue.get('current_complexity', '')}\n"
            f"**Problem:** {issue.get('issue', '')}\n\n"
            f"**Fix:**\n```python\n{issue.get('fix', '')[:400]}\n```\n"
            f"**Improvement:** {issue.get('improvement', '')}\n"
        )

    quick_wins = r.get("quick_wins", [])
    qw_md = (
        "\n".join(f"- {w}" for w in quick_wins[:5])
        if quick_wins
        else "_No quick wins found._"
    )

    return (
        f"## ⚡ Performance Analysis\n\n"
        f"**Rating:** {r_emoji} {rating.capitalize()}\n\n"
        f"**Summary:** {r.get('summary', '')}\n"
        f"{issues_md}\n"
        f"### 🎯 Quick Wins\n{qw_md}"
    )


def _cmd_arch(
    repo: str, issue_number: int, issue: dict, token: str
) -> str:
    context = ""

    if "pull_request" in issue:
        try:
            files     = gh_get(f"/repos/{repo}/pulls/{issue_number}/files", token)
            filenames = [f["filename"] for f in files[:15]]
            context   = "Files changed:\n" + "\n".join(filenames)
        except Exception:
            pass

    if not context:
        context = (
            f"Title: {issue.get('title','')}\n"
            f"Body: {(issue.get('body') or '')[:500]}"
        )

    r, _meta = router.ask(
        "You are a software architect with 15+ years experience. "
        "Review code architecture. JSON only.",
        f"""Review this for architectural issues:

{context}

Check for:
- Layer boundary violations (e.g. core importing from handlers)
- Circular dependencies
- God classes/functions (too many responsibilities)
- Missing abstractions (repeated patterns)
- Tight coupling (hard to test/replace)
- Naming inconsistencies

Return JSON:
{{
  "health": "excellent|good|needs_work|critical",
  "violations": [
    {{
      "type": "layer_violation|circular_import|god_class|tight_coupling|other",
      "severity": "high|medium|low",
      "location": "file or module",
      "description": "what is wrong",
      "recommendation": "how to fix"
    }}
  ],
  "positive_patterns": ["good thing 1", "good thing 2"],
  "refactoring_priority": "immediate|planned|backlog",
  "summary": "2 sentence assessment"
}}""",
        task="arch",
        max_tokens=1500,
    )

    health  = r.get("health", "good")
    h_emoji = {
        "excellent":  "🟢",
        "good":       "🟡",
        "needs_work": "🟠",
        "critical":   "🔴",
    }.get(health, "🟡")

    violations_md = ""
    for v in r.get("violations", [])[:5]:
        sev   = v.get("severity", "medium")
        s_em  = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(sev, "🟡")
        violations_md += (
            f"\n- {s_em} **{v.get('type','').replace('_',' ').title()}** "
            f"— `{v.get('location', '')}`: {v.get('description', '')}\n"
            f"  → {v.get('recommendation', '')}"
        )

    positives = r.get("positive_patterns", [])
    pos_md = (
        "\n".join(f"- ✅ {p}" for p in positives[:3])
        if positives
        else ""
    )

    priority = r.get("refactoring_priority", "planned")
    p_emoji  = {
        "immediate": "🔴",
        "planned":   "🟡",
        "backlog":   "🟢",
    }.get(priority, "🟡")

    return (
        f"## 🏗️ Architecture Review\n\n"
        f"**Health:** {h_emoji} {health.replace('_', ' ').capitalize()}\n"
        f"**Refactoring Priority:** {p_emoji} {priority.capitalize()}\n\n"
        f"**Summary:** {r.get('summary', '')}\n"
        f"\n### Issues Found\n{violations_md or '_No violations found._'}\n"
        f"\n### ✅ Good Patterns\n{pos_md or '_None identified._'}"
    )


def _cmd_release(repo: str, token: str, author: str) -> str:
    """
    /release — Draft a GitHub release from latest commits since last tag.
    Creates a draft release with AI-generated release notes.
    """
    try:
        tags    = gh_get(f"/repos/{repo}/tags?per_page=1", token)
        commits = gh_get(f"/repos/{repo}/commits?per_page=20", token)

        latest_tag     = tags[0]["name"] if tags else "v0.0.0"
        commit_list    = "\n".join(
            f"- {c['commit']['message'].split(chr(10))[0]}"
            for c in commits[:15]
        )

        r, _meta = router.ask(
            "Technical writer. Generate a GitHub release. JSON only.",
            f"""Generate release notes for the next version after {latest_tag}.

Commits since last release:
{commit_list}

Return JSON:
{{
  "version": "next semantic version (e.g. v1.2.3)",
  "title": "short release title",
  "highlights": ["key feature 1", "key feature 2"],
  "breaking_changes": [],
  "release_notes": "full markdown release notes"
}}""",
            task="changelog",
        )

        version        = r.get("version", "v0.0.1")
        release_notes  = r.get("release_notes", "")
        highlights     = r.get("highlights", [])

        # Create draft release via GitHub API
        release = gh_post(f"/repos/{repo}/releases", token, {
            "tag_name":         version,
            "name":             r.get("title", version),
            "body":             release_notes,
            "draft":            True,
            "prerelease":       False,
            "generate_release_notes": False,
        })

        release_url = release.get("html_url", "")
        highlights_md = (
            "\n".join(f"- {h}" for h in highlights[:5])
            if highlights
            else "_No highlights identified._"
        )

        return (
            f"## 🚀 Draft Release Created\n\n"
            f"**Version:** `{version}`\n"
            f"**Status:** Draft (review before publishing)\n\n"
            f"### Highlights\n{highlights_md}\n\n"
            f"[View Draft Release]({release_url})\n\n"
            f"> ✏️ Edit the draft before publishing — "
            f"AI-generated notes may need adjustments."
        )

    except Exception as e:
        return f"## ⚠️ Release creation failed: `{str(e)[:200]}`"


def _cmd_runtests(repo: str, issue_number: int, token: str) -> str:
    """
    /runtests — Trigger CI test workflow via GitHub Actions workflow_dispatch.
    Requires a workflow named 'test.yml' or 'ci.yml' in the repo.
    """
    try:
        repo_data      = gh_get(f"/repos/{repo}", token)
        default_branch = repo_data.get("default_branch", "main")

        # Find test workflow
        workflows = gh_get(f"/repos/{repo}/actions/workflows", token)
        test_workflow = None
        for wf in workflows.get("workflows", []):
            if any(
                name in wf.get("path", "").lower()
                for name in ("test", "ci", "pytest", "check")
            ):
                test_workflow = wf
                break

        if not test_workflow:
            return (
                "## ⚠️ No Test Workflow Found\n\n"
                "Could not find a CI/test workflow in `.github/workflows/`.\n\n"
                "Create a workflow file named `test.yml` or `ci.yml` to enable `/runtests`."
            )

        wf_id = test_workflow["id"]
        gh_post(
            f"/repos/{repo}/actions/workflows/{wf_id}/dispatches",
            token,
            {"ref": default_branch},
        )

        wf_name = test_workflow.get("name", "Test workflow")
        wf_url  = (
            f"https://github.com/{repo}/actions/workflows/"
            f"{test_workflow.get('path','').split('/')[-1]}"
        )

        return (
            f"## 🧪 Tests Triggered\n\n"
            f"**Workflow:** `{wf_name}`\n"
            f"**Branch:** `{default_branch}`\n\n"
            f"[View workflow runs]({wf_url})\n\n"
            f"Results will appear in GitHub Actions within a few minutes."
        )

    except Exception as e:
        return f"## ⚠️ Could not trigger tests: `{str(e)[:200]}`"
