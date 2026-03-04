"""
Issues Handler - app/handlers/issues.py
"""

from app.github.auth import get_installation_token
from app.github.client import gh_get, gh_post, GitHubError
from app.ai.client import groq_ask
from app.ai.validator import validate_issue_triage
from app.core.config import load_config
from app.core.guardrails import check_auto_label
from app.core.logger import EventLogger

SKIP_AUTHORS = {"dependabot[bot]", "renovate[bot]", "github-actions[bot]", "ai-repo-manager[bot]"}


def handle(payload: dict):
    action = payload.get("action")
    if action != "opened":
        return

    issue = payload["issue"]
    if "pull_request" in issue:
        return  # PR events come via pull_request webhook, not issues

    repo = payload["repository"]["full_name"]
    issue_number = issue["number"]
    author = issue["user"]["login"]
    installation_id = payload["installation"]["id"]

    log = EventLogger("issues", repo=repo)

    if author in SKIP_AUTHORS:
        return

    log.info(f"Issue #{issue_number} opened by @{author}")

    try:
        token = get_installation_token(installation_id)
    except Exception as e:
        log.error(f"Auth failed: {e}")
        return

    config = load_config(repo, token)

    if not config.issues_enabled():
        return

    if config.get("labels", "auto_create", default=True):
        try:
            _ensure_labels(repo, token)
        except Exception:
            pass

    # AI triage
    raw = groq_ask(
        "You are an expert open source maintainer. Triage issues. Return valid JSON only.",
        f"""Triage this issue:
Repo: {repo}
Title: {issue.get('title', '')}
Author: {author}
Body: {(issue.get('body') or '')[:1500] or '(empty)'}

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

    result = validate_issue_triage(raw)

    # Build final labels
    priority = result["priority"]
    p_emoji = {"high": "🔥", "medium": "📌", "low": "💤"}.get(priority, "📌")
    all_labels = result["labels"] + [f"priority: {priority} {p_emoji}"]

    # Guardrail: labels
    label_guard = check_auto_label(issue, all_labels, config)
    if label_guard.passed:
        try:
            gh_post(f"/repos/{repo}/issues/{issue_number}/labels", token,
                   {"labels": all_labels})
        except GitHubError:
            pass

    # Build comment
    t_emoji = {"bug": "🐛", "feature": "✨", "question": "❓", "docs": "📚",
               "performance": "⚡", "security": "🔒"}.get(result["type"], "📋")
    c_emoji = {"trivial": "⚡", "simple": "🟢", "moderate": "🟡", "complex": "🔴"}.get(
        result["complexity"], "🟡")

    q_section = ""
    if result["needs_info"] and result["questions"]:
        q_section = "\n\n### ❓ Quick questions\n" + "\n".join(
            f"- {q}" for q in result["questions"][:2])

    comment = f"""## {t_emoji} Thanks for the issue!

{result['welcome']}

| | |
|---|---|
| **Type** | {t_emoji} {result['type'].capitalize()} |
| **Priority** | {p_emoji} {priority.capitalize()} |
| **Complexity** | {c_emoji} {result['complexity'].capitalize()} |
{q_section}

💡 *Commands: `/fix` `/explain` `/improve` `/test` `/docs`*
{config.footer}"""

    try:
        gh_post(f"/repos/{repo}/issues/{issue_number}/comments", token, {"body": comment})
        log.done(f"Issue #{issue_number} triaged as {result['type']}/{priority}")
    except GitHubError as e:
        log.error(f"Could not post comment: {e}")


def _ensure_labels(repo: str, token: str):
    LABELS = [
        ("excellence: approved ✅", "0075ca"), ("excellence: needs work 🔧", "e4e669"),
        ("excellence: critical 🚨", "d93f0b"), ("type: feat ✨", "84b6eb"),
        ("type: fix 🐛", "fc2929"), ("type: refactor ♻️", "fbca04"),
        ("type: docs 📚", "c5def5"), ("type: test 🧪", "bfd4f2"),
        ("priority: high 🔥", "e11d48"), ("priority: medium 📌", "f97316"),
        ("priority: low 💤", "6b7280"), ("bug 🐛", "d73a4a"),
        ("enhancement ✨", "a2eeef"), ("help wanted 🙏", "008672"),
        ("good first issue 👋", "7057ff"),
    ]
    for name, color in LABELS:
        try:
            gh_post(f"/repos/{repo}/labels", token, {"name": name, "color": color})
        except Exception:
            pass
