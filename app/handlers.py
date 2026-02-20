"""
Webhook Event Handlers
Handles all GitHub events: PRs, Issues, Comments, Push
"""

import logging
from app.auth import get_installation_token, gh_get, gh_post, gh_patch, groq_ask, groq_text

log = logging.getLogger(__name__)

BOT_FOOTER = "\n\n---\n*🤖 [GitHub Autopilot](https://github.com/apps/github-autopilot) — AI-powered repo management*"
SKIP_AUTHORS = {"dependabot[bot]", "renovate[bot]", "github-actions[bot]"}


# ─────────────────────────────────────────────────────
# PULL REQUEST HANDLER
# ─────────────────────────────────────────────────────

def handle_pull_request(payload: dict):
    action = payload.get("action")
    if action not in ("opened", "reopened"):
        return

    pr = payload["pull_request"]
    repo = payload["repository"]["full_name"]
    pr_number = pr["number"]
    author = pr["user"]["login"]
    title = pr.get("title", "")
    body = pr.get("body", "") or ""
    installation_id = payload["installation"]["id"]

    if author in SKIP_AUTHORS:
        return

    log.info(f"PR #{pr_number} opened in {repo} by {author}")
    token = get_installation_token(installation_id)

    # Ensure labels exist
    _ensure_labels(repo, token)

    # Get changed files
    try:
        files = gh_get(f"/repos/{repo}/pulls/{pr_number}/files", token)
        file_names = [f["filename"] for f in files[:15]]
        patches = {f["filename"]: f.get("patch", "")[:800] for f in files[:5]}
    except Exception:
        file_names = []
        patches = {}

    files_str = "\n".join(file_names) or "unknown"
    patches_str = "\n\n".join(f"# {k}\n{v}" for k, v in patches.items())

    # AI: Polish PR
    result = groq_ask(
        "You are a principal engineer. Analyze PRs and respond with valid JSON only — no markdown.",
        f"""Analyze this PR:
Title: {title}
Branch: {pr['head']['ref']} → {pr['base']['ref']}
Author: {author}
Body: {body[:500] or '(empty)'}
Files:\n{files_str}
Patches:\n{patches_str[:2000]}

Return JSON:
{{
  "improved_title": "conventional commit title",
  "description": "## 📋 Summary\\n...\\n\\n## 🔄 Changes\\n- ...\\n\\n## 🧪 Testing\\n- ...\\n\\n## ✅ Checklist\\n- [ ] Tests added\\n- [ ] Docs updated\\n- [ ] Self-reviewed",
  "labels": ["type: feat ✨"],
  "risk_level": "low",
  "risk_reason": "why",
  "reviewer_focus": "what to review",
  "pr_type": "feat"
}}"""
    )

    # Update PR title if improved
    patch_data = {}
    if result.get("improved_title") and result["improved_title"] != title:
        patch_data["title"] = result["improved_title"]
    if not body or len(body.strip()) < 30:
        patch_data["body"] = result.get("description", body)

    if patch_data:
        try:
            gh_patch(f"/repos/{repo}/pulls/{pr_number}", token, patch_data)
        except Exception as e:
            log.warning(f"Could not update PR: {e}")

    # Add labels
    labels = result.get("labels", [])
    risk = result.get("risk_level", "low")
    if labels:
        try:
            gh_post(f"/repos/{repo}/issues/{pr_number}/labels", token, {"labels": labels})
        except Exception:
            pass

    # Post comment
    risk_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(risk, "🟡")
    was_updated = bool(patch_data)
    update_note = f"\n\n> 📝 Auto-improved: {'title + description' if 'body' in patch_data else 'title'}" if was_updated else ""

    comment = f"""## 🚀 GitHub Autopilot — PR Analysis

| | |
|---|---|
| **Risk** | {risk_emoji} {risk.capitalize()} — {result.get('risk_reason', '')} |
| **Type** | `{result.get('pr_type', 'unknown')}` |
| **Files** | {len(file_names)} changed |
| **Review Focus** | {result.get('reviewer_focus', 'General review')} |
{update_note}{BOT_FOOTER}"""

    gh_post(f"/repos/{repo}/issues/{pr_number}/comments", token, {"body": comment})

    # Run code review async-style (same request)
    _run_code_review(repo, pr_number, token, files, author)


def _run_code_review(repo, pr_number, token, files, author):
    """AI code review on PR files."""
    REVIEWABLE = {".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".java", ".sql", ".rs"}
    reviewable = [
        f for f in files
        if any(f["filename"].endswith(ext) for ext in REVIEWABLE)
        and f.get("status") != "removed"
        and f.get("changes", 0) > 0
    ][:4]

    if not reviewable:
        return

    reviews = []
    for f in reviewable:
        fname = f["filename"]
        patch = f.get("patch", "")[:1500]
        result = groq_ask(
            "You are a senior engineer. Review code changes. Return valid JSON only.",
            f"""Review this change:
File: {fname}
Patch:
{patch}

Return JSON:
{{
  "score": 7,
  "verdict": "one line",
  "issues": [{{"severity": "major", "issue": "...", "fix": "..."}}],
  "positives": ["..."]
}}""",
            max_tokens=800,
            fast=True
        )
        if result.get("score"):
            reviews.append((fname, result))

    if not reviews:
        return

    avg = sum(r.get("score", 7) for _, r in reviews) / len(reviews)
    all_issues = []
    for _, r in reviews:
        all_issues.extend(r.get("issues", []))

    critical = [i for i in all_issues if i.get("severity") == "critical"]
    score_bar = "█" * int(avg) + "░" * (10 - int(avg))

    verdict = "✅ Good to merge" if avg >= 7.5 else "🟡 Review needed" if avg >= 5 else "🔴 Issues found"

    issues_md = ""
    for issue in all_issues[:6]:
        sev = issue.get("severity", "minor")
        emoji = {"critical": "🚨", "major": "⚠️", "minor": "💡", "nit": "📌"}.get(sev, "💡")
        issues_md += f"\n{emoji} **{sev.upper()}** — {issue.get('issue', '')}"
        if issue.get("fix"):
            issues_md += f"\n```\n{issue['fix'][:200]}\n```"

    file_table = "\n".join(
        f"| `{fname}` | {r.get('score', '?')}/10 | {r.get('verdict', '—')} |"
        for fname, r in reviews
    )

    comment = f"""## 🧠 AI Code Review

**Score: {avg:.1f}/10** `{score_bar}` — {verdict}

### Files Reviewed
| File | Score | Verdict |
|------|-------|---------|
{file_table}
{issues_md or chr(10) + "No major issues found ✅"}
{BOT_FOOTER}"""

    try:
        gh_post(f"/repos/{repo}/issues/{pr_number}/comments", token, {"body": comment})
        # Add critical label if needed
        if critical:
            gh_post(f"/repos/{repo}/issues/{pr_number}/labels", token,
                   {"labels": ["excellence: critical 🚨"]})
    except Exception as e:
        log.warning(f"Review comment failed: {e}")


# ─────────────────────────────────────────────────────
# ISSUES HANDLER
# ─────────────────────────────────────────────────────

def handle_issues(payload: dict):
    action = payload.get("action")
    if action != "opened":
        return

    issue = payload["issue"]
    repo = payload["repository"]["full_name"]
    issue_number = issue["number"]
    author = issue["user"]["login"]
    title = issue.get("title", "")
    body = issue.get("body", "") or ""
    installation_id = payload["installation"]["id"]

    if author in SKIP_AUTHORS or "pull_request" in issue:
        return

    log.info(f"Issue #{issue_number} opened in {repo}")
    token = get_installation_token(installation_id)
    _ensure_labels(repo, token)

    result = groq_ask(
        "You are an expert open source maintainer. Triage issues. Return valid JSON only.",
        f"""Triage this issue:
Repo: {repo}
Title: {title}
Author: {author}
Body: {body[:1500] or '(empty)'}

Return JSON:
{{
  "type": "bug|feature|question|docs|performance|security",
  "priority": "high|medium|low",
  "labels": ["bug 🐛"],
  "welcome": "warm 2-sentence response",
  "needs_info": false,
  "questions": ["clarifying question if needed"],
  "complexity": "trivial|simple|moderate|complex"
}}"""
    )

    # Add labels
    labels = result.get("labels", [])
    priority = result.get("priority", "medium")
    p_emoji = {"high": "🔥", "medium": "📌", "low": "💤"}.get(priority, "📌")
    labels.append(f"priority: {priority} {p_emoji}")
    try:
        gh_post(f"/repos/{repo}/issues/{issue_number}/labels", token, {"labels": labels})
    except Exception:
        pass

    t_emoji = {"bug": "🐛", "feature": "✨", "question": "❓", "docs": "📚",
               "performance": "⚡", "security": "🔒"}.get(result.get("type", ""), "📋")
    c_emoji = {"trivial": "⚡", "simple": "🟢", "moderate": "🟡", "complex": "🔴"}.get(
        result.get("complexity", "moderate"), "🟡")

    questions = result.get("questions", [])
    q_section = ""
    if result.get("needs_info") and questions:
        q_section = "\n\n### ❓ Quick questions\n" + "\n".join(f"- {q}" for q in questions[:2])

    comment = f"""## {t_emoji} Thanks for the issue!

{result.get('welcome', 'Thank you for reporting this!')}

| | |
|---|---|
| **Type** | {t_emoji} {result.get('type', 'issue').capitalize()} |
| **Priority** | {p_emoji} {priority.capitalize()} |
| **Complexity** | {c_emoji} {result.get('complexity', 'moderate').capitalize()} |
{q_section}{BOT_FOOTER}"""

    gh_post(f"/repos/{repo}/issues/{issue_number}/comments", token, {"body": comment})


# ─────────────────────────────────────────────────────
# ISSUE COMMENT HANDLER (Bot Commands)
# ─────────────────────────────────────────────────────

def handle_issue_comment(payload: dict):
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

    COMMANDS = ["/fix", "/explain", "/improve", "/test", "/docs", "/review"]
    cmd = next((c for c in COMMANDS if c in body.lower()), None)
    if not cmd:
        return

    log.info(f"Command {cmd} by @{author} in {repo}#{issue_number}")
    token = get_installation_token(installation_id)

    # Get issue context
    try:
        issue = gh_get(f"/repos/{repo}/issues/{issue_number}", token)
        ctx_title = issue.get("title", "")
        ctx_body = issue.get("body", "") or ""
    except Exception:
        ctx_title, ctx_body = "", ""

    # Extract code block from comment
    import re
    code_match = re.search(r'```[\w]*\n([\s\S]*?)\n```', body)
    code = code_match.group(1) if code_match else ""
    context = re.sub(r'```[\s\S]*?```', '', body).replace(cmd, "").strip()
    full_context = code or context or ctx_body or ctx_title

    response = ""

    if cmd == "/fix":
        r = groq_ask(
            "Senior engineer. Give precise fix. JSON only.",
            f"Fix:\nContext: {ctx_title}\n{full_context[:2000]}\n\nReturn: {{\"root_cause\":\"...\",\"fix\":\"code\",\"explanation\":\"why\",\"test\":\"test code\"}}",
            fast=True
        )
        response = f"## 🔧 Fix\n\n**Root cause:** {r.get('root_cause','')}\n\n**Fix:**\n```\n{r.get('fix','')}\n```\n\n**Why:** {r.get('explanation','')}\n\n**Test:**\n```\n{r.get('test','')}\n```"

    elif cmd == "/explain":
        text = groq_text("Senior engineer and teacher. Explain clearly.", f"Explain:\n{full_context[:2000]}")
        response = f"## 💡 Explanation\n\n{text}"

    elif cmd == "/improve":
        r = groq_ask(
            "Staff engineer. Suggest improvements. JSON only.",
            f"Improve:\n{full_context[:2000]}\n\nReturn: {{\"improvements\":[{{\"area\":\"performance\",\"suggestion\":\"...\",\"example\":\"code\"}}],\"summary\":\"...\"}}",
            fast=True
        )
        imps = r.get("improvements", [])
        lines = [f"## ✨ Improvements\n\n**{r.get('summary','')}**\n"]
        for i, imp in enumerate(imps[:4], 1):
            lines.append(f"### {i}. `{imp.get('area','').upper()}` — {imp.get('suggestion','')}")
            if imp.get("example"):
                lines.append(f"```\n{imp['example'][:300]}\n```")
        response = "\n\n".join(lines)

    elif cmd == "/test":
        r = groq_ask(
            "Senior QA engineer. Generate tests. JSON only.",
            f"Tests for:\n{full_context[:2000]}\n\nReturn: {{\"framework\":\"pytest\",\"tests\":[{{\"name\":\"...\",\"type\":\"unit\",\"code\":\"...\",\"desc\":\"...\"}}]}}",
            fast=True
        )
        tests = r.get("tests", [])
        lines = [f"## 🧪 Tests ({r.get('framework','pytest')})\n"]
        for t in tests[:3]:
            lines.append(f"### `{t.get('name','test')}` ({t.get('type','unit')})\n*{t.get('desc','')}*\n```python\n{t.get('code','')[:400]}\n```")
        response = "\n\n".join(lines)

    elif cmd == "/docs":
        r = groq_ask(
            "Technical writer. Generate docs. JSON only.",
            f"Docs for:\n{full_context[:2000]}\n\nReturn: {{\"docstring\":\"...\",\"usage\":\"...\",\"readme_section\":\"...\"}}",
            fast=True
        )
        response = f"## 📚 Documentation\n\n**Docstring:**\n```\n{r.get('docstring','')}\n```\n\n**Usage:**\n```\n{r.get('usage','')}\n```\n\n**README section:**\n{r.get('readme_section','')}"

    elif cmd == "/review":
        response = "## 🔄 Re-running review...\n\nFull AI review will post shortly."

    if response:
        full = f"{response}\n\n---\n*🤖 `{cmd}` by GitHub Autopilot • @{author} requested*{BOT_FOOTER}"
        gh_post(f"/repos/{repo}/issues/{issue_number}/comments", token, {"body": full})


# ─────────────────────────────────────────────────────
# PUSH HANDLER
# ─────────────────────────────────────────────────────

def handle_push(payload: dict):
    """Check commit messages on push to main."""
    ref = payload.get("ref", "")
    if not any(b in ref for b in ["/main", "/master"]):
        return

    commits = payload.get("commits", [])
    repo = payload["repository"]["full_name"]
    installation_id = payload.get("installation", {}).get("id")
    if not installation_id:
        return

    import re
    CONVENTIONAL = re.compile(
        r'^(feat|fix|docs|style|refactor|perf|test|chore|ci|build|revert)(\(.+\))?(!)?: .+',
        re.IGNORECASE
    )

    bad = [(c["id"][:7], c["message"].split("\n")[0])
           for c in commits[:10]
           if not CONVENTIONAL.match(c["message"].split("\n")[0])
           and not c["message"].startswith("Merge")]

    if not bad:
        return

    log.info(f"Push to {repo} main: {len(bad)} non-conventional commits")

    # Just log — don't spam on every push, only flag
    token = get_installation_token(installation_id)
    try:
        # Create an issue if many bad commits pile up
        if len(bad) >= 3:
            rows = "\n".join(f"| `{sha}` | `{msg[:60]}` |" for sha, msg in bad)
            gh_post(f"/repos/{repo}/issues", token, {
                "title": f"⚡ {len(bad)} non-conventional commits pushed to main",
                "body": f"""## Commit Quality Alert

These commits don't follow [Conventional Commits](https://conventionalcommits.org) spec:

| SHA | Message |
|-----|---------|
{rows}

**Format:** `type(scope): description`
**Types:** feat, fix, docs, refactor, test, chore, perf, ci

Use `/fix` command or the GitHub Autopilot dashboard to fix them.
{BOT_FOOTER}""",
                "labels": ["help wanted 🙏"]
            })
    except Exception as e:
        log.warning(f"Push handler error: {e}")


# ─────────────────────────────────────────────────────
# HELPER: Ensure labels exist
# ─────────────────────────────────────────────────────

def _ensure_labels(repo: str, token: str):
    LABELS = [
        ("excellence: approved ✅", "0075ca"),
        ("excellence: needs work 🔧", "e4e669"),
        ("excellence: critical 🚨", "d93f0b"),
        ("type: feat ✨", "84b6eb"),
        ("type: fix 🐛", "fc2929"),
        ("type: refactor ♻️", "fbca04"),
        ("type: docs 📚", "c5def5"),
        ("type: test 🧪", "bfd4f2"),
        ("priority: high 🔥", "e11d48"),
        ("priority: medium 📌", "f97316"),
        ("priority: low 💤", "6b7280"),
        ("bug 🐛", "d73a4a"),
        ("enhancement ✨", "a2eeef"),
        ("help wanted 🙏", "008672"),
        ("good first issue 👋", "7057ff"),
    ]
    import requests as _req
    for name, color in LABELS:
        try:
            gh_post(f"/repos/{repo}/labels", token, {"name": name, "color": color})
        except Exception:
            pass
