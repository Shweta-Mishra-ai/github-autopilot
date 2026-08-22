"""
app/handlers/pull_request/report.py
Assembly of the single sticky PR comment.

V7 consolidated four separate comments into one. Everything that decides what
that comment *looks like* lives here; everything that decides what goes *in* it
lives in the analysis/review/gaps modules.
"""

from __future__ import annotations

import datetime


def _build_pr_report(
    analysis_md: str,
    summary_md: str,
    review_md: str,
    gaps_md: str,
    pr: dict,
    files: list,
) -> str:
    """
    Assemble the single sticky body.

    Collapsible <details> sections keep the comment scannable: a reviewer sees
    the headline and opens only the section they care about, instead of
    scrolling past four full-length comments.
    """
    adds = sum(f.get("additions", 0) for f in files)
    dels = sum(f.get("deletions", 0) for f in files)

    parts = [
        f"## 🤖 Autopilot — PR #{pr.get('number', '?')}\n",
        f"**Files:** {len(files)} · **+{adds} −{dels}**",
    ]
    if summary_md:
        parts.append(summary_md)
    if analysis_md:
        parts.append(f"<details><summary>📋 Analysis</summary>\n\n{analysis_md}\n</details>")
    if review_md:
        parts.append(f"<details><summary>🔍 Code review</summary>\n\n{review_md}\n</details>")
    if gaps_md:
        parts.append(f"<details><summary>🧪 Test coverage</summary>\n\n{gaps_md}\n</details>")

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts.append(f"\n*Updated {stamp}*")
    return "\n\n".join(parts)
