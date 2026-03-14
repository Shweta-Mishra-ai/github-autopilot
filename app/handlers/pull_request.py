"""
Pull Request Handler - app/handlers/pull_request.py
V3: PR analysis + AI code review + embedding-based context.
"""

import logging
from app.github.auth import get_installation_token
from app.github.client import gh_get, gh_post, gh_put, GitHubError
from app.github.notifications import notify_high_risk_pr
from app.ai.client import groq_ask, groq_text
from app.ai.validator import validate_pr_analysis, validate_code_review
from app.core.config import load_config
from app.core.logger import EventLogger
from app.core.confidence import ConfidenceGate
from app.core.guardrails import check_pr_title_update, check_pr_description_update

SKIP_AUTHORS = {"dependabot[bot]", "renovate[bot]", "github-actions[bot]", "ai-repo-manager[bot]"}


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

    # Get changed files
    try:
        files = gh_get(f"/repos/{repo}/pulls/{pr_number}/files", token)
    except Exception:
        files = []

    # ── Get embedding-based context ───────────────────────────────
    context = ""
    try:
        from app.intelligence.retrieval import get_context_for_pr
        context = get_context_for_pr(repo, files)
        if context:
            log.info("intelligence.context_retrieved")
    except Exception as e:
        log.debug(f"Context retrieval skipped: {e}")

    # ── PR Analysis ───────────────────────────────────────────────
    if action == "opened":
        _analyze_pr(pr, repo, pr_number, files, token, config, gate, context, log)

    # ── Code Review ───────────────────────────────────────────────
    if config.get("pull_requests", "code_review", default=True):
        _review_code(pr, repo, pr_number, files, token, config, gate, context, log)


def _analyze_pr(pr, repo, pr_number, files, token, config, gate, context, log):
    """Run PR analysis: title rewrite, description, risk assessment."""
    title = pr.get("title", "")
    body = pr.get("body", "") or ""
    base_branch = pr["base"]["ref"]
    head_branch = pr["head"]["ref"]

    files_summary = "\n".join(
        f"- {f['filename']} (+{f.get('additions',0)} -{f.get('deletions',0)})"
        for f in files[:8]
    )

    r = groq_ask(
        "Senior engineer. Analyze GitHub PRs. JSON only.",
        f"""Analyze this Pull Request:

Title: {title}
Branch: {head_branch} → {base_branch}
Author: {pr['user']['login']}
Description: {body[:600]}

Changed files:
{files_summary}

{context}

Return JSON:
{{
  "suggested_title": "conventional commit format title",
  "description": "structured PR description with ## Summary, ## Changes, ## Testing",
  "risk_level": "low|medium|high",
  "risk_reason": "why this risk level",
  "review_focus": ["area1", "area2"],
  "confidence": 0.85
}}"""
    )

    r = validate_pr_analysis(r)
    result = gate.evaluate("pr_title_rewrite", r)

    # Post analysis comment
    risk_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(r.get("risk_level", "low"), "🟢")
    focus_items = "\n".join(f"- {f}" for f in r.get("review_focus", [])[:3])
    confidence_note = result.get("confidence_note", "")

    comment = f"""## 🤖 PR Analysis

{risk_emoji} **Risk Level:** `{r.get('risk_level', 'low').upper()}`
**Reason:** {r.get('risk_reason', '')}

### Review Focus
{focus_items}

### Suggested Title
```
{r.get('suggested_title', title)}
```

{f"> ⚠️ {confidence_note}" if confidence_note else ""}
"""

    try:
        gh_post(f"/repos/{repo}/issues/{pr_number}/comments", token, {"body": comment + config.footer})
        log.done("pr_analysis_posted")
    except GitHubError as e:
        log.error(f"Failed to post PR analysis: {e}")

    # Auto-update title if confidence is high enough
    if result["auto_apply"] and r.get("suggested_title"):
        guard = check_pr_title_update(pr, config)
        if guard.passed:
            try:
                gh_put(f"/repos/{repo}/pulls/{pr_number}", token, {
                    "title": r["suggested_title"]
                })
                log.done("pr_title_updated")
            except Exception as e:
                log.error(f"Title update failed: {e}")

    # Notify if high risk
    if r.get("risk_level") == "high":
        try:
            notify_high_risk_pr(repo, pr_number, title)
        except Exception:
            pass


def _review_code(pr, repo, pr_number, files, token, config, gate, context, log):
    """Run AI code review on changed files."""
    max_files = config.get("pull_requests", "max_files_reviewed", default=4)
    reviewable = [
        f for f in files[:max_files]
        if f.get("patch") and not _is_generated(f["filename"])
    ]

    if not reviewable:
        return

    reviews = []

    for f in reviewable:
        filename = f["filename"]
        patch = f.get("patch", "")[:1500]

        r = groq_ask(
            "Senior code reviewer. Give precise, actionable feedback. JSON only.",
            f"""Review this code change:

File: {filename}
Patch:
```
{patch}
```

{context[:800] if context else ""}

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
            fast=True
        )

        r = validate_code_review(r)
        score = r.get("score", 8)
        issues = r.get("issues", [])

        if issues:
            issues_md = "\n".join(
                f"- **{i.get('severity','minor').upper()}** line ~{i.get('line','?')}: "
                f"{i.get('issue','')} → `{i.get('fix','')[:80]}`"
                for i in issues[:4]
            )
        else:
            issues_md = "No issues found."

        reviews.append(
            f"### `{filename}` — Score: {score}/10\n{r.get('summary','')}\n\n{issues_md}"
        )

    if reviews:
        review_body = "## 🔍 AI Code Review\n\n" + "\n\n---\n\n".join(reviews)
        try:
            gh_post(f"/repos/{repo}/issues/{pr_number}/comments", token,
                    {"body": review_body + config.footer})
            log.done(f"code_review_posted for {len(reviews)} files")
        except GitHubError as e:
            log.error(f"Failed to post code review: {e}")


def _is_generated(filename: str) -> bool:
    """Skip generated/binary files."""
    skip_extensions = {
        ".lock", ".sum", ".min.js", ".min.css",
        ".png", ".jpg", ".jpeg", ".gif", ".svg",
        ".pdf", ".zip", ".tar", ".whl"
    }
    return any(filename.endswith(ext) for ext in skip_extensions)
