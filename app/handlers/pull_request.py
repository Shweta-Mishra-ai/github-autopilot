"""
Pull Request Handler - app/handlers/pull_request.py
Handles pull_request webhook events.
Uses: config, guardrails, idempotency, structured logging, AI validation.
"""

import time
import logging
from app.github.auth import get_installation_token
from app.github.client import gh_get, gh_post, gh_patch, gh_put, gh_delete, GitHubError
from app.ai.client import groq_ask, groq_text
from app.ai.validator import validate_pr_analysis, validate_code_review
from app.core.config import load_config
from app.core.guardrails import (
    check_pr_auto_merge, check_title_update,
    check_description_update, check_auto_label
)
from app.core.logger import EventLogger

SKIP_AUTHORS = {"dependabot[bot]", "renovate[bot]", "github-actions[bot]", "ai-repo-manager[bot]"}


def handle(payload: dict):
    action = payload.get("action")
    pr = payload["pull_request"]
    repo = payload["repository"]["full_name"]
    pr_number = pr["number"]
    author = pr["user"]["login"]
    installation_id = payload["installation"]["id"]

    log = EventLogger("pull_request", repo=repo)

    if author in SKIP_AUTHORS:
        return

    if action not in ("opened", "reopened"):
        return

    log.info(f"PR #{pr_number} {action} by @{author}")

    try:
        token = get_installation_token(installation_id)
    except Exception as e:
        log.error(f"Auth failed: {e}")
        return

    # Load repo config (falls back to defaults if no config file)
    config = load_config(repo, token)

    if not config.pr_enabled():
        log.info("PR handling disabled in config — skipping")
        return

    # Ensure labels exist (non-blocking)
    if config.get("labels", "auto_create", default=True):
        try:
            _ensure_labels(repo, token)
        except Exception:
            pass

    # Get changed files
    files = []
    try:
        files = gh_get(f"/repos/{repo}/pulls/{pr_number}/files", token)
        file_names = [f["filename"] for f in files[:15]]
        patches = {f["filename"]: f.get("patch", "")[:800] for f in files[:5]}
    except GitHubError as e:
        log.warning(f"Could not fetch PR files: {e}")
        file_names, patches = [], {}

    # AI Analysis
    raw_result = groq_ask(
        "You are a principal engineer. Analyze PRs and respond with valid JSON only — no markdown.",
        f"""Analyze this PR:
Title: {pr.get('title', '')}
Branch: {pr['head']['ref']} → {pr['base']['ref']}
Author: {author}
Body: {(pr.get('body') or '')[:500]}
Files:\n{chr(10).join(file_names) or 'unknown'}
Patches:\n{chr(10).join(f'# {k}{chr(10)}{v}' for k, v in patches.items())[:2000]}

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

    # Validate AI response — never trust raw output
    result = validate_pr_analysis(raw_result)

    # Guardrail: should we update title?
    title_guard = check_title_update(pr.get("title", ""), result["improved_title"], config)
    desc_guard = check_description_update(pr.get("body", "") or "", config)

    patch_data = {}
    if title_guard.passed:
        patch_data["title"] = result["improved_title"]
        log.info(f"Updating PR title: {result['improved_title'][:60]}")
    else:
        log.debug(f"Title update skipped: {title_guard.reason}")

    if desc_guard.passed and result["description"]:
        patch_data["body"] = result["description"]

    if patch_data:
        try:
            gh_patch(f"/repos/{repo}/pulls/{pr_number}", token, patch_data)
        except GitHubError as e:
            log.warning(f"Could not update PR: {e}")

    # Guardrail: labels
    label_guard = check_auto_label(pr, result["labels"], config)
    if label_guard.passed:
        try:
            gh_post(f"/repos/{repo}/issues/{pr_number}/labels", token,
                   {"labels": result["labels"]})
        except GitHubError:
            pass

    # Post analysis comment
    risk = result["risk_level"]
    risk_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(risk, "🟡")
    update_note = ""
    if patch_data:
        updated = "title + description" if "body" in patch_data else "title"
        update_note = f"\n\n> 📝 Auto-improved: {updated}"

    comment = f"""## 🚀 AI Repo Manager — PR Analysis

| | |
|---|---|
| **Risk** | {risk_emoji} {risk.capitalize()} — {result['risk_reason']} |
| **Type** | `{result['pr_type']}` |
| **Files** | {len(file_names)} changed |
| **Review Focus** | {result['reviewer_focus']} |
{update_note}

💡 *Commands: `/fix` `/explain` `/improve` `/test` `/docs` `/refactor` `/health`*
{config.footer}"""

    try:
        gh_post(f"/repos/{repo}/issues/{pr_number}/comments", token, {"body": comment})
    except GitHubError as e:
        log.error(f"Could not post comment: {e}")

    # Code review (non-blocking)
    if config.get("pull_requests", "code_review", default=True):
        try:
            _run_code_review(repo, pr_number, token, files, config)
        except Exception as e:
            log.warning(f"Code review failed: {e}")

    log.done(f"PR #{pr_number} processed")


def _run_code_review(repo: str, pr_number: int, token: str, files: list, config):
    log = EventLogger("code_review", repo=repo)
    REVIEWABLE = {".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".java", ".sql", ".rs"}
    max_files = config.get("pull_requests", "max_files_reviewed", default=4)

    reviewable = [
        f for f in files
        if any(f["filename"].endswith(ext) for ext in REVIEWABLE)
        and f.get("status") != "removed"
        and f.get("changes", 0) > 0
    ][:max_files]

    if not reviewable:
        return

    reviews = []
    for f in reviewable:
        fname = f["filename"]
        patch = f.get("patch", "")[:1500]
        raw = groq_ask(
            "You are a senior engineer. Review code changes. Return valid JSON only.",
            f"""Review this change:
File: {fname}
Patch:\n{patch}

Return JSON:
{{
  "score": 7,
  "verdict": "one line",
  "issues": [{{"severity": "major", "issue": "...", "fix": "..."}}],
  "positives": ["..."],
  "refactor_opportunity": "optional improvement without behavior change"
}}""",
            max_tokens=800,
            fast=True
        )
        validated = validate_code_review(raw)
        if validated["score"] is not None:
            reviews.append((fname, validated))

    if not reviews:
        return

    avg = sum(r["score"] for _, r in reviews) / len(reviews)
    all_issues = [i for _, r in reviews for i in r["issues"]]
    critical = [i for i in all_issues if i["severity"] == "critical"]

    score_bar = "█" * int(avg) + "░" * (10 - int(avg))
    verdict = "✅ Good to merge" if avg >= 7.5 else "🟡 Review needed" if avg >= 5 else "🔴 Issues found"

    issues_md = ""
    for issue in all_issues[:6]:
        sev = issue["severity"]
        emoji = {"critical": "🚨", "major": "⚠️", "minor": "💡", "nit": "📌"}.get(sev, "💡")
        issues_md += f"\n{emoji} **{sev.upper()}** — {issue['issue']}"
        if issue.get("fix"):
            issues_md += f"\n```\n{issue['fix'][:200]}\n```"

    refactor_opps = [r.get("refactor_opportunity") for _, r in reviews if r.get("refactor_opportunity")]
    refactor_md = ""
    if refactor_opps:
        refactor_md = "\n\n### ♻️ Refactor Opportunities (behavior unchanged)\n"
        refactor_md += "\n".join(f"- {r}" for r in refactor_opps[:3])

    file_table = "\n".join(
        f"| `{fname}` | {r['score']:.1f}/10 | {r['verdict']} |"
        for fname, r in reviews
    )

    comment = f"""## 🧠 AI Code Review

**Score: {avg:.1f}/10** `{score_bar}` — {verdict}

| File | Score | Verdict |
|------|-------|---------|
{file_table}
{issues_md or chr(10) + "No major issues found ✅"}
{refactor_md}
{config.footer}"""

    gh_post(f"/repos/{repo}/issues/{pr_number}/comments", token, {"body": comment})

    if critical:
        gh_post(f"/repos/{repo}/issues/{pr_number}/labels", token,
               {"labels": ["excellence: critical 🚨"]})

    log.done(f"Code review done for PR #{pr_number}")


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
        ("conflict: needs resolution ⚔️", "b60205"),
        ("bug 🐛", "d73a4a"),
        ("enhancement ✨", "a2eeef"),
        ("help wanted 🙏", "008672"),
        ("good first issue 👋", "7057ff"),
    ]
    for name, color in LABELS:
        try:
            gh_post(f"/repos/{repo}/labels", token, {"name": name, "color": color})
        except Exception:
            pass
