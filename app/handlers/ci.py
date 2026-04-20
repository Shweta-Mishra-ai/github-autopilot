"""
app/handlers/ci.py
V4 Sprint 4: CI check_run event handler.

Triggers when a CI check completes.
On failure: posts analysis comment with AI-suggested fix.
On success: posts encouragement only if previously failed.
"""

import logging
from app.github.auth import get_installation_token
from app.github.client import gh_get, gh_post, GitHubError
from app.ai.router import router
from app.core.config import load_config
from app.core.logger import EventLogger

log = logging.getLogger(__name__)

SKIP_CONCLUSIONS = {"skipped", "neutral", "cancelled"}


def handle(payload: dict):
    action = payload.get("action")
    if action not in ("completed",):
        return

    check_run       = payload.get("check_run", {})
    conclusion      = check_run.get("conclusion", "")
    check_name      = check_run.get("name", "")
    repo            = payload["repository"]["full_name"]
    installation_id = payload.get("installation", {}).get("id")

    if not installation_id:
        return
    if conclusion in SKIP_CONCLUSIONS:
        return

    log_ctx = EventLogger("ci", repo=repo)

    # Only handle failures
    if conclusion != "failure":
        return

    log_ctx.info(f"CI failure: {check_name}")

    try:
        token = get_installation_token(installation_id)
    except Exception as e:
        log_ctx.error(f"Auth failed: {e}")
        return

    config = load_config(repo, token)
    if not config.get("ci", "enabled", default=True):
        return

    # Get check run details — logs, output
    check_id   = check_run.get("id", "")
    output     = check_run.get("output", {})
    title      = output.get("title", "")
    summary    = output.get("summary", "")[:2000]
    details    = output.get("text", "")[:3000]

    # Find associated PR
    pull_requests = check_run.get("pull_requests", [])
    if not pull_requests:
        log_ctx.info(f"No PR associated with check run — skipping comment")
        return

    pr_number = pull_requests[0]["number"]

    # Build context for AI
    failure_context = f"""CI Check: {check_name}
Conclusion: {conclusion}
Title: {title}
Summary: {summary}
Details: {details}"""

    try:
        r, _meta = router.ask(
            "Senior DevOps engineer. Analyze CI failures concisely. JSON only.",
            f"""Analyze this CI failure and suggest a fix:

{failure_context}

Return JSON:
{{
  "root_cause": "one sentence — exact reason",
  "category": "test_failure|build_error|lint_error|dependency|timeout|other",
  "fix": "concrete steps to fix — 2-4 bullet points",
  "is_flaky": false,
  "confidence": 0.8
}}""",
            task="ci_analysis",
        )

        category  = r.get("category", "other")
        cat_emoji = {
            "test_failure": "🧪",
            "build_error":  "🏗️",
            "lint_error":   "🔍",
            "dependency":   "📦",
            "timeout":      "⏱️",
            "other":        "❌",
        }.get(category, "❌")

        flaky_note = ""
        if r.get("is_flaky"):
            flaky_note = "\n\n> 🎲 **Possibly flaky** — this might pass on re-run."

        fix_text = r.get("fix", "")
        if fix_text and not fix_text.startswith("-"):
            fix_text = "- " + fix_text.replace("\n", "\n- ")

        comment = f"""## {cat_emoji} CI Failure — `{check_name}`

**Root cause:** {r.get('root_cause', 'See details below')}

### Fix
{fix_text}
{flaky_note}

---
*🤖 AI Repo Manager V4 — CI Analysis*{config.footer}"""

        gh_post(f"/repos/{repo}/issues/{pr_number}/comments", token,
                {"body": comment})
        log_ctx.done(f"CI failure comment posted PR #{pr_number}")

    except Exception as e:
        log_ctx.error(f"CI handler failed: {e}")

