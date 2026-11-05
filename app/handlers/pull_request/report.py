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
    notices_md: str = "",
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
    from app.github.helpers import PR_FILES_MAX_PAGES, PR_FILES_PER_PAGE

    if len(files) >= PR_FILES_MAX_PAGES * PR_FILES_PER_PAGE:
        parts.append(
            f"> ℹ️ GitHub lists at most {len(files):,} files for a pull request. "
            "This report covers those; any beyond them were not seen."
        )
    if notices_md:
        parts.append(notices_md)
    # The risk is the line a reviewer needs first, and it was folded inside
    # "Analysis" under the summary. Its block (level, reason, what raised it)
    # is shown open; the review focus and suggested title stay folded.
    headline, _, analysis_rest = (analysis_md or "").strip().partition("\n\n")
    if "**Risk Level:**" in headline:
        parts.append(headline)
    else:
        headline, analysis_rest = "", (analysis_md or "").strip()
    if summary_md:
        parts.append(summary_md)
    if analysis_rest.strip():
        parts.append(
            f"<details><summary>📋 Analysis</summary>\n\n{analysis_rest.strip()}\n</details>"
        )
    if review_md:
        parts.append(f"<details><summary>🔍 Code review</summary>\n\n{review_md}\n</details>")
    if gaps_md:
        rows = sum(1 for line in gaps_md.splitlines() if line.startswith("| `"))
        label = (
            f"🧪 Test coverage — {rows} gap{'s' if rows != 1 else ''}"
            if rows
            else "🧪 Test coverage"
        )
        parts.append(f"<details><summary>{label}</summary>\n\n{gaps_md}\n</details>")

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts.append(f"\n*Updated {stamp}*")
    return "\n\n".join(parts)
