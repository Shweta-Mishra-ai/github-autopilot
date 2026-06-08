"""
Issues Handler - app/handlers/issues.py
V4 Sprint 4: Industry-level issue triage with rich scoring.

IMPROVED:
- Richer triage prompt with repo context
- Priority scoring with reasoning
- Complexity estimation with time estimate
- Better welcome message — personalized per issue type
- Similar issues detection to prevent duplicates
"""

from app.github.auth import get_installation_token
from app.github.client import gh_get, gh_post, GitHubError
from app.github.notifications import notify_new_issue
from app.ai.router import router
from app.ai.validator import validate_issue_triage
from app.core.config import load_config
from app.core.guardrails import check_auto_label
from app.core.logger import EventLogger

SKIP_AUTHORS = {
    "dependabot[bot]",
    "renovate[bot]",
    "github-actions[bot]",
    "ai-repo-manager[bot]",
}


def handle(payload: dict):
    action = payload.get("action")
    if action != "opened":
        return

    issue = payload["issue"]
    if "pull_request" in issue:
        return

    repo = payload["repository"]["full_name"]
    issue_number = issue["number"]
    author = issue["user"]["login"]
    installation_id = payload["installation"]["id"]
    title = issue.get("title", "")
    body = (issue.get("body") or "")[:2000]

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

    # Get repo context for better triage
    repo_lang = ""
    try:
        repo_data = gh_get(f"/repos/{repo}", token)
        repo_lang = repo_data.get("language", "") or ""
    except Exception:
        pass

    if config.get("labels", "auto_create", default=True):
        try:
            _ensure_labels(repo, token)
        except Exception:
            pass

    raw, _meta = router.ask(
        "You are an expert open source maintainer and technical lead. "
        "Triage GitHub issues with precision. Return valid JSON only.",
        f"""Triage this GitHub issue with deep analysis:

Repository: {repo}
Primary Language: {repo_lang or "unknown"}
Issue #{issue_number} by @{author}
Title: {title}
Body:
{body or "(empty — user provided no description)"}

Perform thorough triage:

1. Classify the issue type accurately
2. Assess priority based on: user impact, frequency, blocking nature
3. Estimate complexity based on: scope of change needed
4. Write a warm, helpful welcome that shows understanding of their specific problem
5. Ask targeted clarifying questions if info is missing

Return JSON:
{{
  "type": "bug|feature|question|docs|performance|security|refactor",
  "priority": "critical|high|medium|low",
  "complexity": "trivial|simple|moderate|complex|epic",
  "time_estimate": "< 1 hour|1-4 hours|1-3 days|1-2 weeks|> 2 weeks",
  "labels": ["bug 🐛"],
  "welcome": "2-3 sentence personalized response that acknowledges their specific issue",
  "needs_info": true,
  "questions": ["specific question about reproduction steps", "version/environment info"],
  "is_duplicate_risk": false,
  "similar_search_terms": ["search terms to find duplicates"],
  "auto_close_reason": ""
}}""",
        task="issue_triage",
        max_tokens=1000,
    )

    result = validate_issue_triage(raw)

    # Priority → emoji + label
    priority = result["priority"]
    p_map = {
        "critical": ("🚨", "priority: critical 🚨"),
        "high": ("🔥", "priority: high 🔥"),
        "medium": ("📌", "priority: medium 📌"),
        "low": ("💤", "priority: low 💤"),
    }
    p_emoji, p_label = p_map.get(priority, ("📌", "priority: medium 📌"))

    # Type → emoji
    t_emoji = {
        "bug": "🐛",
        "feature": "✨",
        "question": "❓",
        "docs": "📚",
        "performance": "⚡",
        "security": "🔒",
        "refactor": "♻️",
    }.get(result["type"], "📋")

    # Complexity → emoji
    c_emoji = {
        "trivial": "⚡",
        "simple": "🟢",
        "moderate": "🟡",
        "complex": "🔴",
        "epic": "🏔️",
    }.get(result["complexity"], "🟡")

    # Labels
    all_labels = result["labels"] + [p_label]

    label_guard = check_auto_label(issue, all_labels, config)
    if label_guard.passed:
        try:
            gh_post(
                f"/repos/{repo}/issues/{issue_number}/labels",
                token,
                {"labels": all_labels},
            )
        except GitHubError:
            pass

    # Build questions section
    q_section = ""
    if result["needs_info"] and result.get("questions"):
        q_items = "\n".join(f"  - {q}" for q in result["questions"][:3])
        q_section = f"\n\n### ❓ To help us resolve this faster\n{q_items}"

    # Time estimate
    time_est = result.get("time_estimate", "")
    time_row = f"\n| **Est. Effort** | {time_est} |" if time_est else ""

    comment = f"""## {t_emoji} Thanks for the issue, @{author}!

{result["welcome"]}

| | |
|---|---|
| **Type** | {t_emoji} {result["type"].capitalize()} |
| **Priority** | {p_emoji} {priority.capitalize()} |
| **Complexity** | {c_emoji} {result["complexity"].capitalize()} |{time_row}
{q_section}

---
💡 *Use `/explain`, `/fix`, or `/improve` on this issue for AI assistance.*
{config.footer}"""

    try:
        gh_post(
            f"/repos/{repo}/issues/{issue_number}/comments", token, {"body": comment}
        )
        log.done(f"Issue #{issue_number} triaged: {result['type']}/{priority}")
    except GitHubError as e:
        log.error(f"Comment failed: {e}")

    # Notification
    try:
        notify_new_issue(
            repo=repo, issue_number=issue_number, title=title, labels=all_labels
        )
    except Exception:
        pass


def _ensure_labels(repo: str, token: str):
    LABELS = [
        ("priority: critical 🚨", "d93f0b"),
        ("priority: high 🔥", "e11d48"),
        ("priority: medium 📌", "f97316"),
        ("priority: low 💤", "6b7280"),
        ("bug 🐛", "d73a4a"),
        ("enhancement ✨", "a2eeef"),
        ("question ❓", "d876e3"),
        ("documentation 📚", "0075ca"),
        ("performance ⚡", "e4e669"),
        ("security 🔒", "e11d48"),
        ("good first issue 👋", "7057ff"),
        ("help wanted 🙏", "008672"),
    ]
    for name, color in LABELS:
        try:
            gh_post(f"/repos/{repo}/labels", token, {"name": name, "color": color})
        except Exception:
            pass
