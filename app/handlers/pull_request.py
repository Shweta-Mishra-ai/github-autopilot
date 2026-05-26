"""
Pull Request Handler - app/handlers/pull_request.py
V3: PR analysis + AI code review + embedding-based context
    + AI PR Summary auto-post + Test gap detection

FIXED (ruff F401 line 7):  Removed unused `import logging`.
FIXED (ruff F401 line 16): Removed unused `check_pr_description_update` import.
"""

from app.github.auth import get_installation_token
from app.github.client import gh_get, gh_post, gh_put, GitHubError
from app.github.notifications import notify_high_risk_pr, notify_pr_opened
from app.ai.router import router
from app.ai.validator import validate_pr_analysis, validate_code_review
from app.core.config import load_config
from app.core.logger import EventLogger
from app.core.confidence import ConfidenceGate
from app.core.guardrails import check_pr_title_update

SKIP_AUTHORS = {
    "dependabot[bot]",
    "renovate[bot]",
    "github-actions[bot]",
    "ai-repo-manager[bot]",
}


def handle(payload: dict):
    action = payload.get("action")
    if action not in ("opened", "reopened", "synchronize"):
        return

    pr = payload["pull_request"]
    repo = payload["repository"]["full_name"]
    installation_id = payload["installation"]["id"]
    author = pr["user"]["login"]
    pr_number = pr["number"]

    log = EventLogger("pull_request", repo=repo, pr=pr_number)

    if author in SKIP_AUTHORS or author.endswith("[bot]"):
        return

    try:
        token = get_installation_token(installation_id)
    except Exception as e:
        log.error(f"Auth failed: {e}")
        return

    config = load_config(repo, token)
    gate = ConfidenceGate(config)

    if not config.pr_enabled():
        return

    try:
        files = gh_get(f"/repos/{repo}/pulls/{pr_number}/files", token)
    except Exception:
        files = []

    context = ""
    try:
        from app.intelligence.retrieval import get_context_for_pr

        context = get_context_for_pr(repo, files)
        if context:
            log.info("intelligence.context_retrieved")
    except Exception as e:
        log.debug(f"Context retrieval skipped: {e}")

    if action == "opened":
        try:
            notify_pr_opened(
                repo=repo,
                pr_number=pr_number,
                title=pr.get("title", ""),
                risk="unknown",
            )
        except Exception:
            pass

        _analyze_pr(pr, repo, pr_number, files, token, config, gate, context, log)
        _post_pr_summary(pr, repo, pr_number, files, token, config, log)

    if config.get("pull_requests", "code_review", default=True):
        _review_code(pr, repo, pr_number, files, token, config, gate, context, log)

    if config.get("pull_requests", "detect_test_gaps", default=True):
        _detect_test_gaps(pr, repo, pr_number, files, token, config, log)


def _analyze_pr(pr, repo, pr_number, files, token, config, gate, context, log):
    """Run PR analysis: title rewrite, description, risk assessment."""
    title = pr.get("title", "")
    body = pr.get("body", "") or ""
    base_branch = pr["base"]["ref"]
    head_branch = pr["head"]["ref"]

    files_summary = "\n".join(
        f"- {f['filename']} (+{f.get('additions', 0)} -{f.get('deletions', 0)})"
        for f in files[:8]
    )

    r, _meta = router.ask(
        "Senior engineer. Analyze GitHub PRs. JSON only.",
        f"""Analyze this Pull Request:

Title: {title}
Branch: {head_branch} → {base_branch}
Author: {pr["user"]["login"]}
Description: {body[:600]}

Changed files:
{files_summary}

{context[:800] if context else ""}

Return JSON:
{{
  "suggested_title": "conventional commit format title",
  "description": "structured PR description with ## Summary, ## Changes, ## Testing sections",
  "risk_level": "low|medium|high",
  "risk_reason": "why this risk level",
  "review_focus": ["area1", "area2"],
  "confidence": 0.85
}}""",
        task="pr_analysis",
    )

    r = validate_pr_analysis(r)
    result = gate.evaluate("pr_title_rewrite", r)

    risk_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(
        r.get("risk_level", "low"), "🟢"
    )
    focus_items = "\n".join(f"- {f}" for f in r.get("review_focus", [])[:3])
    confidence_note = result.get("confidence_note", "")

    comment = f"""## 🤖 PR Analysis

{risk_emoji} **Risk Level:** `{r.get("risk_level", "low").upper()}`
**Reason:** {r.get("risk_reason", "")}

### Review Focus
{focus_items}

### Suggested Title
```
{r.get("suggested_title", title)}
```

{f"> ⚠️ {confidence_note}" if confidence_note else ""}
"""

    try:
        gh_post(
            f"/repos/{repo}/issues/{pr_number}/comments",
            token,
            {"body": comment + config.footer},
        )
        log.done("pr_analysis_posted")
    except GitHubError as e:
        log.error(f"Failed to post PR analysis: {e}")

    if result["auto_apply"] and r.get("suggested_title"):
        guard = check_pr_title_update(pr, config)
        if guard.passed:
            try:
                gh_put(
                    f"/repos/{repo}/pulls/{pr_number}",
                    token,
                    {"title": r["suggested_title"]},
                )
                log.done("pr_title_updated")
            except Exception as e:
                log.error(f"Title update failed: {e}")

    if r.get("risk_level") == "high":
        try:
            notify_high_risk_pr(repo, pr_number, title)
        except Exception:
            pass


def _post_pr_summary(pr, repo, pr_number, files, token, config, log):
    """Auto-generate and post a human-readable PR summary on open."""
    try:
        title = pr.get("title", "")
        body = pr.get("body", "") or ""

        files_list = "\n".join(
            f"- {f.get('filename', '')} (+{f.get('additions', 0)} -{f.get('deletions', 0)})"
            for f in files[:10]
        )

        total_additions = sum(f.get("additions", 0) for f in files)
        total_deletions = sum(f.get("deletions", 0) for f in files)

        summary, _meta = router.ask_text(
            "Senior engineer. Write clear, concise PR summaries for reviewers.",
            f"""Write a reviewer-friendly summary for this Pull Request.

Title: {title}
Author: {pr["user"]["login"]}
Base branch: {pr["base"]["ref"]}
Description: {body[:500]}

Changed files ({len(files)} total, +{total_additions} -{total_deletions} lines):
{files_list}

Write 3-5 sentences covering:
1. What this PR accomplishes
2. Key technical changes made
3. What reviewers should focus on
Keep it concise and helpful.""",
            task="pr_summary",
        )

        comment = f"""## 📋 PR Summary

{summary}

| Stat | Value |
|------|-------|
| 📁 Files changed | {len(files)} |
| ➕ Lines added | {total_additions} |
| ➖ Lines removed | {total_deletions} |
"""

        gh_post(
            f"/repos/{repo}/issues/{pr_number}/comments",
            token,
            {"body": comment + config.footer},
        )
        log.done("pr_summary_posted")

    except Exception as e:
        log.error(f"PR summary failed: {e}")


def _detect_test_gaps(pr, repo, pr_number, files, token, config, log):
    """Detect test coverage gaps in changed files."""
    try:
        source_files = [
            f
            for f in files
            if f.get("filename", "").endswith((".py", ".js", ".ts"))
            and not _is_test_file(f.get("filename", ""))
            and f.get("patch")
        ]

        test_files = [f for f in files if _is_test_file(f.get("filename", ""))]

        if not source_files:
            return

        source_context = "\n\n".join(
            f"### {f['filename']}\n```\n{f.get('patch', '')[:600]}\n```"
            for f in source_files[:4]
        )

        test_context = (
            "\n".join(f"- {f['filename']}" for f in test_files)
            or "No test files changed in this PR."
        )

        r, _meta = router.ask(
            "Senior QA engineer. Identify test gaps precisely. JSON only.",
            f"""Analyze these code changes for test coverage gaps:

Changed source files:
{source_context}

Test files changed in this PR:
{test_context}

Return JSON:
{{
  "has_gaps": true,
  "coverage_score": 6,
  "gaps": [
    {{
      "file": "filename.py",
      "function": "function_name",
      "risk": "high|medium|low",
      "suggested_test": "describe the test to add"
    }}
  ],
  "summary": "brief overall assessment"
}}

Only report real gaps. If tests are adequate, set has_gaps to false.""",
            task="gaps",
        )

        if not r.get("has_gaps", False):
            log.info("test_gaps.none_found", pr=pr_number)
            return

        gaps = r.get("gaps", [])
        if not gaps:
            return

        gaps_md = "\n".join(
            f"| `{g.get('file', '?')}` | `{g.get('function', '?')}` | "
            f"`{g.get('risk', 'medium')}` | {g.get('suggested_test', '')[:80]} |"
            for g in gaps[:5]
        )

        score = r.get("coverage_score", 5)
        score_emoji = "🟢" if score >= 8 else "🟡" if score >= 5 else "🔴"

        comment = f"""## 🔍 Test Coverage Analysis

{score_emoji} **Coverage Score: {score}/10**
{r.get("summary", "")}

### Gaps Found

| File | Function | Risk | Suggested Test |
|------|----------|------|----------------|
{gaps_md}

> 💡 Use `/gaps` command for a detailed test gap analysis.
> 💡 Use `/test` command to auto-generate missing tests.
"""

        gh_post(
            f"/repos/{repo}/issues/{pr_number}/comments",
            token,
            {"body": comment + config.footer},
        )
        log.done(f"test_gaps_posted: {len(gaps)} gaps found")

    except Exception as e:
        log.error(f"Test gap detection failed: {e}")


def _review_code(pr, repo, pr_number, files, token, config, gate, context, log):
    """Run AI code review on changed files."""
    max_files = config.get("pull_requests", "max_files_reviewed", default=4)
    reviewable = [
        f
        for f in files[:max_files]
        if f.get("patch") and not _is_generated(f["filename"])
    ]

    if not reviewable:
        return

    reviews = []

    for f in reviewable:
        filename = f["filename"]
        patch = f.get("patch", "")[:1500]

        r, _meta = router.ask(
            "Senior code reviewer. Give precise, actionable feedback. JSON only.",
            f"""Review this code change:

File: {filename}
Patch:
```
{patch}
```

{context[:600] if context else ""}

Return JSON:
{{
  "score": 8,
  "issues": [
    {{
      "severity": "critical|major|minor|nit",
      "line": "approximate line",
      "issue": "what is wrong",
      "fix": "exact fix"
    }}
  ],
  "summary": "overall assessment",
  "confidence": 0.80
}}""",
            task="code_review",
        )

        r = validate_code_review(r)
        score = r.get("score", 8)
        issues = r.get("issues", [])

        if issues:
            issues_md = "\n".join(
                f"- **{i.get('severity', 'minor').upper()}** ~line {i.get('line', '?')}: "
                f"{i.get('issue', '')} → `{i.get('fix', '')[:80]}`"
                for i in issues[:4]
            )
        else:
            issues_md = "✅ No issues found."

        reviews.append(
            f"### `{filename}` — Score: {score}/10\n"
            f"{r.get('summary', '')}\n\n{issues_md}"
        )

    if reviews:
        review_body = "## 🔍 AI Code Review\n\n" + "\n\n---\n\n".join(reviews)
        try:
            gh_post(
                f"/repos/{repo}/issues/{pr_number}/comments",
                token,
                {"body": review_body + config.footer},
            )
            log.done(f"code_review_posted: {len(reviews)} files")
        except GitHubError as e:
            log.error(f"Failed to post code review: {e}")


def _is_test_file(filename: str) -> bool:
    return (
        "test_" in filename
        or "_test." in filename
        or "/tests/" in filename
        or filename.startswith("test")
    )


def _is_generated(filename: str) -> bool:
    skip_extensions = {
        ".lock",
        ".sum",
        ".min.js",
        ".min.css",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".pdf",
        ".zip",
        ".tar",
        ".whl",
    }
    return any(filename.endswith(ext) for ext in skip_extensions)


def _blast_radius(files: list) -> str:
    """
    Categorize changed files into system layers for blast radius display.
    Used by /impact command in comments.py.
    Returns a markdown string summarizing which layers are affected.
    """
    categories: dict[str, list[str]] = {
        "Handlers (API layer)": [],
        "Core (foundation)": [],
        "AI (LLM layer)": [],
        "Security": [],
        "Tests": [],
        "Config / Deploy": [],
        "Documentation": [],
        "Other": [],
    }

    for f in files:
        name = f.get("filename", "")
        if name.startswith("tests/") or name.startswith("test_"):
            categories["Tests"].append(name)
        elif name.startswith("app/handlers/"):
            categories["Handlers (API layer)"].append(name)
        elif name.startswith("app/core/"):
            categories["Core (foundation)"].append(name)
        elif name.startswith("app/ai/"):
            categories["AI (LLM layer)"].append(name)
        elif name.startswith("app/security/"):
            categories["Security"].append(name)
        elif name.endswith((".yml", ".yaml", ".toml", "Procfile",
                             "Dockerfile", "requirements.txt", "render.yaml")):
            categories["Config / Deploy"].append(name)
        elif name.endswith((".md", ".rst", ".txt")):
            categories["Documentation"].append(name)
        else:
            categories["Other"].append(name)

    lines = []
    for layer, layer_files in categories.items():
        if layer_files:
            sample = ", ".join(f"`{f.split('/')[-1]}`" for f in layer_files[:3])
            more   = f" +{len(layer_files) - 3} more" if len(layer_files) > 3 else ""
            lines.append(f"- **{layer}** — {sample}{more}")

    return "\n".join(lines) if lines else "- No categorized files found"
