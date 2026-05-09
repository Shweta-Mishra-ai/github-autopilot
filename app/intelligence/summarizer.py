"""
app/intelligence/summarizer.py
V4 Sprint 7: Upgraded PR/issue summarizer.

V3 was too generic. V4:
  - Structured 5-section summary
  - Risk-aware summary (flags high-risk changes)
  - Reviewer-specific tips based on file types changed
"""

import logging

from app.ai.router import router

log = logging.getLogger(__name__)


def summarize_pr(
    pr: dict = None,
    files: list = None,
    context: str = "",
    # Support legacy signature: summarize_pr(title, body, files, repo)
    title: str = None,
    body: str = "",
    repo: str = "",
) -> str:
    """
    Generate a structured, reviewer-friendly PR summary.
    Returns markdown string.

    Supports two signatures:
    - summarize_pr(pr={"title": ..., "body": ...}, files=[...], context="...")
    - summarize_pr(title="...", body="...", files=[...], repo="...")
    """
    # Handle legacy signature
    if pr is None and title is not None:
        pr = {"title": title, "body": body}
    elif pr is None:
        pr = {}
    if files is None:
        files = []

    try:
        title    = pr.get("title", "")
        body     = (pr.get("body") or "")[:800]
        author   = pr.get("user", {}).get("login", "")
        base     = pr.get("base", {}).get("ref", "main")
        head     = pr.get("head", {}).get("ref", "")

        total_add = sum(f.get("additions", 0) for f in files)
        total_del = sum(f.get("deletions", 0) for f in files)
        file_list = "\n".join(
            f"  {f['filename']} (+{f.get('additions',0)} -{f.get('deletions',0)})"
            for f in files[:10]
        )

        # Classify file types for reviewer tips
        has_tests    = any("test" in f.get("filename","") for f in files)
        has_security = any(
            any(x in f.get("filename","") for x in ["auth", "security", "crypto", "token", "secret"])
            for f in files
        )
        has_deps     = any(f.get("filename","") in ("requirements.txt","package.json","Pipfile")
                          for f in files)

        text, _meta = router.ask_text(
            "Senior engineer. Write concise, structured PR summaries for busy reviewers.",
            f"""Summarize this PR for reviewers:

Title: {title}
Author: @{author}
Branch: {head} → {base}
Description: {body}

Files changed ({len(files)} files, +{total_add} -{total_del}):
{file_list}

{f"Codebase context: {context[:400]}" if context else ""}

Write a structured summary with these exact sections:
## 📋 What This PR Does
(1-2 sentences: the main purpose)

## 🔑 Key Changes
(2-4 bullet points: most important technical changes)

## 🎯 Review Focus
(1-2 things reviewers should pay close attention to)

{"## ⚠️ Security Review Needed" + chr(10) + "(Note security-sensitive files changed)" if has_security else ""}
{"## ⚠️ Dependency Changes" + chr(10) + "(Note dependency file changes)" if has_deps else ""}
{"## ✅ Tests Included" if has_tests else "## ⚠️ No Tests Found"}
(Coverage note)""",
            task="pr_summary",
            max_tokens=600,
        )
        return text

    except Exception as e:
        log.error(f"summarize_pr failed: {e}")
        return ""


def summarize_issue_thread(comments: list, issue: dict) -> str:
    """Summarize a long issue discussion thread."""
    try:
        title     = issue.get("title", "")
        thread    = "\n\n".join(
            f"@{c.get('user',{}).get('login','?')}: {c.get('body','')[:300]}"
            for c in comments[:20]
        )

        text, _ = router.ask_text(
            "Senior engineer. Summarize GitHub discussions concisely.",
            f"""Summarize this issue discussion:

Issue: {title}

Thread:
{thread[:3000]}

Write a brief summary (3-5 sentences):
- What the issue is about
- Key points raised
- Current status / resolution (if any)
- Any action items""",
            task="explain",
            max_tokens=400,
        )
        return text

    except Exception as e:
        log.error(f"summarize_issue_thread failed: {e}")
        return ""
