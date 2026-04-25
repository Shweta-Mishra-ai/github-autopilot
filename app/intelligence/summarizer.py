"""
Summarizer - app/intelligence/summarizer.py
V3: AI-powered summarization for PRs, issues, and discussions.
Uses repo context from embeddings for better summaries.
"""

from app.ai.client import groq_text
from app.core.logger import get_logger

log = get_logger(__name__)


def summarize_pr(
    title: str,
    description: str,
    changed_files: list[dict],
    comments: list[dict],
    context: str = "",
) -> str:
    """
    Generate a concise PR summary with key changes and decisions.
    """
    files_list = "\n".join(
        f"- {f.get('filename', '')} (+{f.get('additions', 0)} -{f.get('deletions', 0)})"
        for f in changed_files[:10]
    )

    comments_text = "\n".join(
        f"@{c.get('user', {}).get('login', '')}: {c.get('body', '')[:150]}"
        for c in comments[:10]
    )

    prompt = f"""Summarize this Pull Request concisely.

Title: {title}
Description: {description[:500]}

Changed files:
{files_list}

Discussion:
{comments_text[:1000]}

{context}

Write a 3-5 sentence summary covering:
1. What this PR does
2. Key files changed
3. Any important decisions or concerns raised
"""

    try:
        summary = groq_text(
            "Senior engineer. Write clear, concise PR summaries.", prompt
        )
        log.info("summarizer.pr_done")
        return summary
    except Exception as e:
        log.error("summarizer.pr_failed", error=str(e))
        return "Could not generate summary."


def summarize_issue_thread(title: str, body: str, comments: list[dict]) -> str:
    """
    Summarize a long issue discussion thread.
    """
    thread = "\n\n".join(
        f"@{c.get('user', {}).get('login', '')}: {c.get('body', '')[:300]}"
        for c in comments[:20]
    )

    prompt = f"""Summarize this GitHub issue discussion.

Issue: {title}
Description: {body[:400]}

Discussion:
{thread[:2500]}

Write a summary covering:
1. The core problem or request
2. Key points raised in discussion
3. Current status or decision reached (if any)
"""

    try:
        summary = groq_text(
            "Senior engineer. Summarize GitHub discussions clearly.", prompt
        )
        log.info("summarizer.issue_done")
        return summary
    except Exception as e:
        log.error("summarizer.issue_failed", error=str(e))
        return "Could not generate summary."


def summarize_ci_failure(logs: str) -> str:
    """
    Analyze CI failure logs and explain root cause + fix.
    """
    prompt = f"""Analyze this CI failure log and provide:
1. Root cause (1 sentence)
2. Exact fix (code or commands)
3. Prevention tip

CI Logs:
{logs[:3000]}
"""
    try:
        return groq_text("DevOps expert. Diagnose CI failures precisely.", prompt)
    except Exception as e:
        log.error("summarizer.ci_failed", error=str(e))
        return "Could not analyze CI failure."
