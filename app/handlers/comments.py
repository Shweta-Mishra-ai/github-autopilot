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
from app.github.helpers import fmt_error
from app.security.enhanced_secrets import (
    format_findings as format_secret_findings,
    scan_diff,
)
from app.security.dependencies import scan_requirements_txt, format_dep_findings
import logging
_log = logging.getLogger(__name__)

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




def _extract_command(body: str):
    """
    Word-boundary command extraction.
    Fixes substring bug: '/autofix' previously matched '/apply' first.
    Uses negative lookbehind so '/fix' won't match 'prefix' or 'proactive'.
    """
    body_lower = body.lower()
    for cmd in ALL_COMMANDS:
        if re.search(r'(?<![/\w])' + re.escape(cmd) + r'\b', body_lower):
            return cmd
    return None




def _safe_router_ask(system: str, user: str, task: str,
                     max_tokens: int = 1000) -> tuple[dict, object]:
    """
    Wrapper around router.ask() with consistent error handling.
    Returns (result_dict, meta). On any failure returns ({}, None).
    Callers check: if not result: return fmt_error(...)
    """
    try:
        return router.ask(system, user, task=task, max_tokens=max_tokens)
    except Exception as e:
        _log.error(f"router.ask failed task={task}: {e}")
        return {}, None


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

    cmd = _extract_command(body)
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
            response = _cmd_ci(full_context, repo=repo, token=token)
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


# ── Shared helpers (used by multiple commands) ─────────────────────────────

def _fetch_commits_since_tag(
    repo: str, token: str, per_page: int = 20
) -> tuple[list, str]:
    """
    Fetch recent commits and the latest tag name.
    Returns (commits_list, latest_tag_str).
    Shared by /changelog and /release to avoid duplicated GitHub API calls.
    """
    tags       = gh_get(f"/repos/{repo}/tags?per_page=1", token)
    commits    = gh_get(f"/repos/{repo}/commits?per_page={per_page}", token)
    latest_tag = tags[0]["name"] if (isinstance(tags, list) and tags) else "v0.0.0"
    return (commits if isinstance(commits, list) else []), latest_tag


def _bump_version(version: str) -> str:
    """
    Increment the patch segment of a semver string.
    "v1.2.3" → "v1.2.4"
    Falls back to "v0.1.0" if parsing fails.
    """
    try:
        m = re.match(r"^(v?)(\d+)\.(\d+)\.(\d+)", version.strip())
        if m:
            prefix, major, minor, patch = m.group(1), m.group(2), m.group(3), m.group(4)
            return f"{prefix}{major}.{minor}.{int(patch) + 1}"
    except Exception:
        pass
    return "v0.1.0"


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
    """
    /apply <branch> — Create a PR from an autofix branch to default branch.

    Usage:
      /apply                     -> lists available fix/bot-issue-* branches
      /apply fix/bot-issue-42    -> opens a PR from that branch
    """
    branch = context.strip()

    # Guard: reject obviously unsafe branch names
    if branch and (
        ".." in branch
        or branch.startswith("/")
        or " " in branch
        or len(branch) > 200
    ):
        return (
            "## \u26a0\ufe0f Invalid Branch Name\n\n"
            f"`{branch[:80]}` is not a valid branch name.\n\n"
            "Usage: `/apply fix/bot-issue-42`"
        )

    try:
        repo_data      = gh_get(f"/repos/{repo}", token)
        default_branch = repo_data.get("default_branch", "main")

        # No branch given: list available autofix branches
        if not branch:
            branches   = gh_get(f"/repos/{repo}/branches?per_page=100", token)
            fix_branches = [
                b["name"] for b in (branches if isinstance(branches, list) else [])
                if b.get("name", "").startswith("fix/bot-issue-")
            ]
            if not fix_branches:
                return (
                    "## \u2139\ufe0f No Autofix Branches Found\n\n"
                    "No `fix/bot-issue-*` branches exist yet.\n\n"
                    "Run `/autofix` on an issue first, then use "
                    "`/apply <branch>` to create the PR."
                )
            branch_list = "\n".join(f"- `{b}`" for b in fix_branches[:10])
            return (
                "## \U0001f331 Available Autofix Branches\n\n"
                f"{branch_list}\n\n"
                "Reply with `/apply <branch-name>` to open a PR."
            )

        # Branch given: verify it exists
        try:
            gh_get(f"/repos/{repo}/branches/{branch}", token)
        except GitHubError as e:
            if e.status_code == 404:
                return (
                    f"## \u26a0\ufe0f Branch Not Found\n\n"
                    f"`{branch}` does not exist in `{repo}`.\n\n"
                    "Use `/apply` (no args) to see available branches."
                )
            raise

        # Check if PR already exists for this branch
        owner = repo.split("/")[1] if "/" in repo else repo
        existing_prs = gh_get(
            f"/repos/{repo}/pulls?head={owner}:{branch}&state=open&per_page=5",
            token,
        )
        if isinstance(existing_prs, list) and existing_prs:
            pr = existing_prs[0]
            return (
                f"## \u2139\ufe0f PR Already Exists\n\n"
                f"A PR for `{branch}` is already open: "
                f"[#{pr['number']} \u2014 {pr['title'][:60]}]({pr['html_url']})"
            )

        # Derive issue number from branch name (fix/bot-issue-42)
        issue_ref = ""
        m = re.search(r"issue-(\d+)", branch)
        if m:
            issue_ref = f"\n\nCloses #{m.group(1)}"

        # Create the PR
        pr = gh_post(f"/repos/{repo}/pulls", token, {
            "title": f"fix: autofix for issue #{issue_number}",
            "head":  branch,
            "base":  default_branch,
            "body": (
                f"## \U0001f916 Autofix PR\n\n"
                f"Requested by `/apply` on issue #{issue_number}.\n"
                f"Branch: `{branch}` \u2192 `{default_branch}`"
                f"{issue_ref}\n\n"
                "> \u26a0\ufe0f AI-generated \u2014 please review all changes before merging."
            ),
            "draft": False,
        })

        pr_url    = pr.get("html_url", "")
        pr_number = pr.get("number", "?")

        return (
            f"## \u2705 PR Created\n\n"
            f"**PR #{pr_number}:** [{pr.get('title','')}]({pr_url})\n\n"
            f"**Branch:** `{branch}` \u2192 `{default_branch}`\n\n"
            f"> Review the changes carefully before merging."
        )

    except GitHubError as e:
        if e.status_code == 422:
            return (
                f"## \u26a0\ufe0f Cannot Create PR\n\n"
                f"GitHub returned 422: `{str(e)[:200]}`\n\n"
                "Possible reasons:\n"
                "- Branch is already up to date with base\n"
                "- A closed PR already exists for this branch\n"
                "- No commits between branch and base"
            )
        return f"## \u26a0\ufe0f Apply Failed\n\n`{str(e)[:200]}`"
    except Exception as e:
        _log.error(f"_cmd_apply unexpected error: {e}")
        return f"## \u26a0\ufe0f Apply Failed\n\nUnexpected error: `{str(e)[:200]}`"


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
        return fmt_error("Health Check Failed", e)


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
        return fmt_error("Version check failed", e)


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
            # Audit log — /merge is irreversible, always record it
            try:
                from app.core.redis_client import get_redis as _get_redis
                import json as _j
                import time as _t
                _get_redis().lpush("audit:merge", _j.dumps({
                    "repo": repo, "pr": issue_number,
                    "by": author, "at": int(_t.time()),
                    "sha": result.get("sha", "")[:12],
                }))
                _get_redis().ltrim("audit:merge", 0, 999)
            except Exception:
                pass  # audit failure must not block the merge
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
        return fmt_error("Merge error", e)


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
        return fmt_error("Summarize failed", e)


def _cmd_ci(context: str, repo: str = "", token: str = "") -> str:
    """
    /ci [context] — Analyze a CI failure.

    If context is provided (e.g. pasted error log): analyze it directly.
    If no context: fetch the latest failed workflow run from GitHub API.
    """
    ci_context = context.strip() if context else ""

    # ── No context: fetch latest failed run from GitHub ──────────────────
    if not ci_context and repo and token:
        try:
            runs = gh_get(
                f"/repos/{repo}/actions/runs?status=failure&per_page=5",
                token,
            )
            run_list = runs.get("workflow_runs", []) if isinstance(runs, dict) else []
            if not run_list:
                return (
                    "## ℹ️ No Recent CI Failures\n\n"
                    "No failed workflow runs found in the last 5 runs.\n\n"
                    "To analyze a specific failure, paste the error log after `/ci`:\n"
                    "```\n/ci\n<paste error output here>\n```"
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
        except Exception as e:
            return (
                f"## ⚠️ Could not fetch CI runs\n\n"
                f"`{str(e)[:200]}`\n\n"
                "To analyze a specific failure, paste the error log after `/ci`."
            )
    elif not ci_context:
        return (
            "## ℹ️ No CI Context\n\n"
            "To analyze a CI failure, either:\n"
            "1. Paste the error log after `/ci`\n"
            "2. Use `/ci` in a repo with GitHub Actions configured\n\n"
            "```\n/ci\n<paste error output here>\n```"
        )

    # ── Analyze the context ───────────────────────────────────────────────
    try:
        r, _meta = router.ask(
            "DevOps expert. Analyze CI failures precisely. JSON only.",
            f"""Analyze this CI failure and provide actionable fixes:

{ci_context[:3000]}

Return JSON:
{{
  "root_cause": "exact reason for failure in one sentence",
  "fix": "step-by-step commands or config changes to fix this",
  "prevention": "how to prevent this in future",
  "confidence": 0.85
}}""",
            task="ci_analysis",
        )

        if not isinstance(r, dict) or "root_cause" not in r:
            return (
                "## ⚠️ CI Analysis Incomplete\n\n"
                "Could not parse a structured response. "
                "Raw output:\n\n"
                f"```\n{str(r)[:500]}\n```"
            )

        conf_pct = int(float(r.get("confidence", 0.85)) * 100)
        return (
            f"## 🔴 CI Failure Analysis\n\n"
            f"**Root Cause:** {r.get('root_cause', 'Unknown')}\n\n"
            f"**Fix:**\n```\n{r.get('fix', 'No fix suggested')}\n```\n\n"
            f"**Prevention:** {r.get('prevention', 'N/A')}\n\n"
            f"*Confidence: {conf_pct}%*"
        )

    except Exception as e:
        _log.error(f"_cmd_ci LLM error: {e}")
        return fmt_error("CI Analysis Failed", e)


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
        return fmt_error("Security scan failed", e)


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
    """
    /changelog — Generate a Keep-a-Changelog entry from recent commits.
    Posts the entry as a comment. Does not modify CHANGELOG.md.
    """
    try:
        commits, latest_tag = _fetch_commits_since_tag(repo, token)

        if not commits:
            return (
                "## ℹ️ No Commits Found\n\n"
                "No commits found in this repository yet."
            )

        commit_list = "\n".join(
            f"- {c['commit']['message'].split(chr(10))[0][:120]}"
            for c in commits[:15]
        )

        if not commit_list.strip():
            return (
                "## ℹ️ No New Commits\n\n"
                f"No new commits since `{latest_tag}`."
            )

        changelog, _meta = router.ask_text(
            "Technical writer. Generate a clean CHANGELOG entry. "
            "Keep a Changelog format. No extra commentary.",
            f"""Generate a CHANGELOG.md entry for the version after {latest_tag}.

Recent commits:
{commit_list}

Format exactly:
## [X.Y.Z] - YYYY-MM-DD
### Added
- ...
### Changed
- ...
### Fixed
- ...

Skip sections with no entries. Use today's date.""",
            task="changelog",
        )

        if not changelog or not changelog.strip():
            return "## ⚠️ Changelog generation returned empty response. Try again."

        return (
            f"## 📋 CHANGELOG Entry\n\n"
            f"```markdown\n{changelog.strip()}\n```\n\n"
            f"*Copy this into your `CHANGELOG.md` before the previous entry.*"
        )

    except GitHubError as e:
        return fmt_error("Changelog failed (GitHub API)", e)
    except Exception as e:
        _log.error(f"_cmd_changelog error: {e}")
        return fmt_error("Changelog generation failed", e)


def _cmd_budget() -> str:
    try:
        from app.ai.metrics import format_budget_comment
        return format_budget_comment()
    except Exception as e:
        return fmt_error("Budget check failed", e)


def _cmd_rollback(
    repo: str, issue_number: int, token: str,
    cmd_args: str, author: str
) -> str:
    """
    /rollback           → list available snapshots
    /rollback 3         → ask for confirmation
    /rollback 3 confirm → execute rollback of snapshot #3

    Two-step confirmation prevents accidental destructive actions.
    Safety snapshot is taken BEFORE rollback; if it fails, rollback aborts.
    """
    from app.core.snapshot import (
        get_snapshot_by_number,
        format_snapshot_list,
        format_rollback_result,
        take_snapshot,
    )

    args = cmd_args.strip() if cmd_args else ""

    # ── No args: show snapshot list ───────────────────────────────────────
    if not args:
        return format_snapshot_list(repo)

    # ── Parse: "3" or "3 confirm" ─────────────────────────────────────────
    parts   = args.split()
    n_str   = parts[0]
    confirm = len(parts) > 1 and parts[1].lower() == "confirm"

    try:
        n = int(n_str)
    except ValueError:
        return (
            f"## ⚠️ Invalid Snapshot Number\n\n"
            f"`{n_str}` is not a valid number.\n\n"
            "Usage:\n"
            "- `/rollback` — see available snapshots\n"
            "- `/rollback 3` — preview snapshot #3\n"
            "- `/rollback 3 confirm` — execute rollback"
        )

    snap = get_snapshot_by_number(repo, n)
    if not snap:
        return (
            f"## ⚠️ Snapshot #{n} Not Found\n\n"
            "Use `/rollback` to see available snapshots "
            "(max 10, expire after 7 days)."
        )

    # ── Show confirmation prompt if not confirmed ─────────────────────────
    bot_actions = snap.get("bot_actions", [])
    snap_ts     = snap.get("timestamp", "")[:16].replace("T", " ")
    action_preview = "\n".join(
        f"- `{a.get('type','unknown')}` on #{a.get('number','?')}"
        for a in bot_actions[:5]
    ) or "- No recorded actions"

    if not confirm:
        return (
            f"## ⚠️ Confirm Rollback\n\n"
            f"**Snapshot #{n}** — taken at `{snap_ts}` "
            f"by trigger: `{snap.get('trigger','unknown')}`\n\n"
            f"**Actions that will be undone:**\n{action_preview}\n\n"
            f"{'*(and more...)*' if len(bot_actions) > 5 else ''}\n\n"
            f"**To proceed:** reply `/rollback {n} confirm`\n"
            f"**To cancel:** ignore this message"
        )

    # ── Confirmed: take safety snapshot first ────────────────────────────
    try:
        take_snapshot(
            repo, token, trigger=f"pre_rollback_by_{author}"
        )
    except Exception as e:
        # Safety snapshot failed — abort. Never rollback without safety net.
        _log.error(f"_cmd_rollback safety snapshot failed: {e}")
        return (
            "## ⚠️ Rollback Aborted\n\n"
            "Could not create a safety snapshot before rolling back.\n\n"
            f"Error: `{str(e)[:200]}`\n\n"
            "Rollback was **not** performed. Fix the snapshot system first."
        )

    # ── Execute rollback ──────────────────────────────────────────────────
    restored: list[str] = []
    failed:   list[str] = []

    for action in reversed(bot_actions):
        action_type = action.get("type", "")
        num         = action.get("number")

        try:
            if action_type == "create_issue" and num:
                gh_put(f"/repos/{repo}/issues/{num}", token, {"state": "closed"})
                restored.append(
                    f"Closed issue #{num}: {action.get('title','')[:50]}"
                )

            elif action_type == "edit_pr_title" and num:
                old_title = action.get("old_title", "")
                if old_title:
                    gh_put(f"/repos/{repo}/pulls/{num}", token, {"title": old_title})
                    restored.append(f"Reverted PR #{num} title to: `{old_title[:50]}`")
                else:
                    failed.append(f"edit_pr_title #{num}: no old_title recorded")

            elif action_type == "add_labels" and num:
                labels = action.get("labels", [])
                label_errors = []
                for lbl in labels:
                    try:
                        gh_delete(f"/repos/{repo}/issues/{num}/labels/{lbl}", token)
                    except GitHubError as le:
                        # 404 = label already removed, that's fine
                        if le.status_code != 404:
                            label_errors.append(f"{lbl}: {str(le)[:40]}")
                    except Exception as le:
                        label_errors.append(f"{lbl}: {str(le)[:40]}")

                if label_errors:
                    failed.append(f"remove labels from #{num}: {'; '.join(label_errors)}")
                else:
                    restored.append(f"Removed labels {labels} from #{num}")

            else:
                # Unknown or incomplete action — skip, don't fail
                _log.warning(f"_cmd_rollback: unknown action type {action_type!r}, skipping")

        except GitHubError as exc:
            failed.append(
                f"{action_type} #{num or '?'}: {str(exc)[:80]}"
            )
        except Exception as exc:
            _log.error(f"_cmd_rollback action {action_type} failed unexpectedly: {exc}")
            failed.append(
                f"{action_type} #{num or '?'}: unexpected error {str(exc)[:60]}"
            )

    if not bot_actions:
        restored.append("No automated actions were recorded in this snapshot")

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
        return fmt_error("Impact analysis failed", e)


def _cmd_secfull(repo: str, token: str) -> str:
    try:
        from app.security.scanner import run_security_scan
        report = run_security_scan(repo, token)
        return report.to_markdown(include_low=True)
    except Exception as e:
        return fmt_error("Security scan failed", e)


def _cmd_autofix(
    repo: str, issue_number: int, issue: dict,
    token: str, cmd_args: str
) -> str:
    from app.handlers.autofix import run_autofix
    target_file = cmd_args.strip() if cmd_args else ""
    return run_autofix(repo, issue_number, issue, token, target_file)


def _cmd_report(repo: str) -> str:
    """
    /report — Show weekly analytics for this repo.
    Degrades gracefully when Redis is unavailable.
    """
    # record_command_used is best-effort — never block the report on it
    try:
        from app.core.analytics import record_command_used
        record_command_used(repo, "report")
    except Exception:
        pass  # Non-critical — analytics tracking failure must not block report

    try:
        from app.core.analytics import format_report_comment
        report = format_report_comment(repo)

        if not report or not report.strip():
            return (
                "## 📊 No Data Yet\n\n"
                "No activity has been recorded for this repo yet.\n\n"
                "The report will populate after the first PR merge, "
                "issue close, or command is used."
            )
        return report

    except Exception as e:
        err = str(e).lower()
        if "redis" in err or "connection" in err or "refused" in err:
            return (
                "## ⚠️ Report Unavailable\n\n"
                "Redis is not reachable — analytics data cannot be read.\n\n"
                "Check your `REDIS_URL` environment variable in Render.\n"
                "The report will work once Redis is connected."
            )
        _log.error(f"_cmd_report error: {e}")
        return fmt_error("Report failed", e)


def _cmd_notify(
    repo: str, issue_number: int, issue: dict,
    token: str, cmd_args: str
) -> str:
    """
    /notify [message] — Send Discord/Slack notification about this issue or PR.

    Checks webhook env vars upfront so the error message is actionable.
    Supports custom message via cmd_args.
    """
    import os
    discord_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    slack_url   = os.environ.get("SLACK_WEBHOOK_URL", "")

    if not discord_url and not slack_url:
        return (
            "## ⚠️ Notifications Not Configured\n\n"
            "No webhook URL found. Add one of these to your Render environment:\n\n"
            "- `DISCORD_WEBHOOK_URL` — Discord webhook URL\n"
            "- `SLACK_WEBHOOK_URL` — Slack webhook URL\n\n"
            "See [Render env vars](https://render.com/docs/environment-variables) "
            "for setup instructions."
        )

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
        custom_msg = cmd_args.strip() if cmd_args else ""

        # Determine color by label semantics
        color = 0x5865F2  # default: Discord blurple
        for lb in labels:
            lb_lower = lb.lower()
            if "bug" in lb_lower or "security" in lb_lower or "critical" in lb_lower:
                color = 0xE74C3C  # red
                break
            if "feature" in lb_lower or "enhancement" in lb_lower:
                color = 0x2ECC71  # green
                break
            if "question" in lb_lower or "help" in lb_lower:
                color = 0xF39C12  # orange
                break

        desc_parts = [
            f"**Repo:** `{repo}`",
            f"**Labels:** {', '.join(labels) or 'none'}",
        ]
        if custom_msg:
            desc_parts.append(f"**Note:** {custom_msg[:200]}")

        notify_title = f"🔔 {kind} #{issue_number} — {title[:80]}"

        success, msg = send_rich_discord(
            title=notify_title,
            description="\n".join(desc_parts),
            color=color,
            fields=[
                {"name": "Type",   "value": kind,               "inline": True},
                {"name": "Number", "value": f"#{issue_number}",  "inline": True},
                {"name": "Repo",   "value": repo,                "inline": False},
            ],
            url=url,
        )

        channels = []
        if discord_url:
            channels.append("Discord")
        if slack_url:
            channels.append("Slack")

        if success:
            return (
                f"## 🔔 Notification Sent\n\n"
                f"Alert posted to: **{', '.join(channels)}**\n\n"
                f"**{kind} #{issue_number}:** {title[:80]}"
            )

        # send_rich_discord returned False
        return (
            f"## ⚠️ Notification Failed\n\n"
            f"Webhook returned error: `{msg[:200]}`\n\n"
            "Check that your webhook URL is valid and the channel still exists."
        )

    except Exception as e:
        _log.error(f"_cmd_notify error: {e}")
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
    /release — Draft a GitHub release from commits since last tag.

    Guards:
    - Empty repo (no commits) → clear message
    - Duplicate tag (422) → suggest next version
    - LLM returns bad version format → fallback bump
    """
    try:
        # Fetch tags and commits
        tags    = gh_get(f"/repos/{repo}/tags?per_page=10", token)
        commits = gh_get(f"/repos/{repo}/commits?per_page=20", token)

        if not commits:
            return (
                "## ⚠️ No Commits Found\n\n"
                "This repository has no commits yet. "
                "Make at least one commit before creating a release."
            )

        # All existing tag names — pass to LLM to avoid conflicts
        existing_tags = [t["name"] for t in (tags if isinstance(tags, list) else [])]
        latest_tag    = existing_tags[0] if existing_tags else "v0.0.0"

        commit_list = "\n".join(
            f"- {c['commit']['message'].split(chr(10))[0][:120]}"
            for c in commits[:15]
        )

        r, _meta = router.ask(
            "Technical writer. Generate a GitHub release. JSON only.",
            f"""Generate release notes for the next version after {latest_tag}.

Existing tags (DO NOT reuse any of these): {', '.join(existing_tags[:10]) or 'none'}
Commits since last release:
{commit_list}

Return JSON:
{{
  "version": "next semantic version e.g. v1.2.3 — must not be in existing tags",
  "title": "short descriptive release title",
  "highlights": ["key change 1", "key change 2"],
  "breaking_changes": [],
  "release_notes": "full markdown release notes"
}}""",
            task="changelog",
        )

        version = r.get("version", "").strip()

        # Validate version format — fallback to auto-bump if LLM fails
        if not version or not re.match(r"^v\d+\.\d+\.\d+", version):
            version = _bump_version(latest_tag)
            _log.warning(f"_cmd_release: LLM returned bad version, using {version}")

        # If LLM suggested an existing tag, bump it
        if version in existing_tags:
            version = _bump_version(version)
            _log.warning(f"_cmd_release: version conflict, bumped to {version}")

        release_notes = r.get("release_notes", f"Release {version}")
        highlights    = r.get("highlights", [])

        # Create draft release
        try:
            release = gh_post(f"/repos/{repo}/releases", token, {
                "tag_name":               version,
                "name":                   r.get("title", version),
                "body":                   release_notes,
                "draft":                  True,
                "prerelease":             False,
                "generate_release_notes": False,
            })
        except GitHubError as e:
            if e.status_code == 422:
                # Tag already exists (race condition or LLM conflict)
                bumped = _bump_version(version)
                return (
                    f"## ⚠️ Tag Already Exists\n\n"
                    f"`{version}` already exists as a tag or release.\n\n"
                    f"Try again — the bot will use `{bumped}` next time, "
                    f"or specify manually by editing the draft."
                )
            raise

        release_url   = release.get("html_url", "")
        highlights_md = (
            "\n".join(f"- {h}" for h in highlights[:5])
            if highlights else "_No highlights identified._"
        )
        breaking      = r.get("breaking_changes", [])
        breaking_md   = (
            "\n".join(f"- ⚠️ {b}" for b in breaking[:3])
            if breaking else ""
        )

        out = (
            f"## 🚀 Draft Release Created\n\n"
            f"**Version:** `{version}`  |  **Status:** Draft\n\n"
            f"### Highlights\n{highlights_md}\n"
        )
        if breaking_md:
            out += f"\n### ⚠️ Breaking Changes\n{breaking_md}\n"
        out += (
            f"\n[View & Edit Draft]({release_url})\n\n"
            f"> Review and publish when ready. "
            f"AI-generated notes may need adjustments."
        )
        return out

    except GitHubError as e:
        return f"## ⚠️ Release creation failed (GitHub API): `{str(e)[:200]}`"
    except Exception as e:
        _log.error(f"_cmd_release error: {e}")
        return f"## ⚠️ Release creation failed: `{str(e)[:200]}`"


def _cmd_runtests(repo: str, issue_number: int, token: str) -> str:
    """
    /runtests — Trigger CI test workflow via GitHub Actions workflow_dispatch.

    Error handling:
    - 422: workflow exists but has no workflow_dispatch trigger → tell user how to fix
    - 403: GitHub App missing actions:write permission → tell user how to fix
    - No workflow found → suggest workflow names to create
    """
    try:
        repo_data      = gh_get(f"/repos/{repo}", token)
        default_branch = repo_data.get("default_branch", "main")

        workflows_data = gh_get(f"/repos/{repo}/actions/workflows", token)
        all_workflows  = (
            workflows_data.get("workflows", [])
            if isinstance(workflows_data, dict) else []
        )

        # Find best matching test/CI workflow
        TEST_NAMES = ("test", "ci", "pytest", "check", "lint", "build")
        test_workflow = None
        for wf in all_workflows:
            path = wf.get("path", "").lower()
            name = wf.get("name", "").lower()
            if any(n in path or n in name for n in TEST_NAMES):
                test_workflow = wf
                break

        if not test_workflow:
            wf_names = [w.get("name", w.get("path", "?")) for w in all_workflows[:5]]
            existing = (
                f"\nExisting workflows: {', '.join(f'`{n}`' for n in wf_names)}"
                if wf_names else ""
            )
            return (
                "## ⚠️ No Test Workflow Found\n\n"
                "Could not find a test/CI workflow in `.github/workflows/`."
                f"{existing}\n\n"
                "Create a workflow file to enable `/runtests`. Example:\n\n"
                "```yaml\n# .github/workflows/test.yml\n"
                "on:\n  push:\n  workflow_dispatch:  # ← required for /runtests\njobs:\n"
                "  test:\n    runs-on: ubuntu-latest\n    steps:\n"
                "      - uses: actions/checkout@v4\n"
                "      - run: pip install -r requirements.txt && pytest\n```"
            )

        wf_id   = test_workflow["id"]
        wf_name = test_workflow.get("name", "Test workflow")
        wf_file = test_workflow.get("path", "").split("/")[-1]
        wf_url  = f"https://github.com/{repo}/actions/workflows/{wf_file}"

        try:
            gh_post(
                f"/repos/{repo}/actions/workflows/{wf_id}/dispatches",
                token,
                {"ref": default_branch},
            )
        except GitHubError as e:
            if e.status_code == 422:
                return (
                    f"## ⚠️ Workflow Cannot Be Dispatched\n\n"
                    f"**Workflow:** `{wf_name}` (`{wf_file}`)\n\n"
                    "This workflow does not have a `workflow_dispatch` trigger.\n\n"
                    f"Add this to `{wf_file}`:\n\n"
                    "```yaml\non:\n  push:\n  workflow_dispatch:  # ← add this line\n```\n\n"
                    "Then commit and try `/runtests` again."
                )
            if e.status_code == 403:
                return (
                    "## ⚠️ Permission Denied\n\n"
                    "The GitHub App does not have `actions: write` permission.\n\n"
                    "Fix in your GitHub App settings:\n"
                    "1. Go to your GitHub App → Permissions & Events\n"
                    "2. Set **Actions** to **Read & Write**\n"
                    "3. Re-install the app on this repo"
                )
            raise

        return (
            f"## 🧪 Tests Triggered\n\n"
            f"**Workflow:** `{wf_name}`\n"
            f"**Branch:** `{default_branch}`\n\n"
            f"[View workflow runs]({wf_url})\n\n"
            f"Results will appear in GitHub Actions within a few minutes."
        )

    except GitHubError as e:
        return f"## ⚠️ Could not trigger tests (GitHub API): `{str(e)[:200]}`"
    except Exception as e:
        _log.error(f"_cmd_runtests error: {e}")
        return f"## ⚠️ Could not trigger tests: `{str(e)[:200]}`"
