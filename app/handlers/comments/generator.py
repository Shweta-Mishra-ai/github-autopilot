"""
app/handlers/comments/generator.py
AI-driven comment generation commands:
  /fix, /explain, /improve, /test, /docs, /refactor, /gaps, /perf, /arch
"""

from __future__ import annotations

import logging

from app.ai.hallucination import add_confidence_footer, check_response
from .dispatcher import safe_router_ask

log = logging.getLogger(__name__)


def cmd_fix(ctx_title: str, context: str, repo: str = "") -> str:
    """Generate a precise bug fix with root cause, code, and test."""
    from app.handlers.comments import router

    # Learned conventions: what this repo previously accepted via /apply//merge.
    # Empty string when nothing has been learned yet — prompt is unchanged then.
    learned = ""
    if repo:
        try:
            from app.core.learning import get_pattern_summary

            learned = get_pattern_summary(repo)
        except Exception:
            learned = ""

    r, _ = router.ask(
        "Senior engineer. Give precise, working fix. JSON only.",
        f"""Fix this issue:
Title: {ctx_title}
Context: {context[:2000]}
{f"Repo conventions (learned from previously accepted fixes):{learned}" if learned else ""}

Return JSON:
{{
  "root_cause": "exact reason",
  "fix": "working code or commit fixes",
  "explanation": "why this fix works",
  "test": "test to verify fix",
  "confidence": 0.85
}}""",
        task="fix_command",
    )
    comment = (
        f"## 🔧 Fix\n\n"
        f"**Root cause:** {r.get('root_cause', 'See fix below')}\n\n"
        f"**Fix:**\n```\n{r.get('fix', '')}\n```\n\n"
        f"**Why:** {r.get('explanation', '')}\n\n"
        f"**Test:**\n```\n{r.get('test', '')}\n```"
    )
    return add_confidence_footer(comment, check_response(r, response_type="fix"))


def cmd_explain(context: str) -> str:
    """Explain an issue or code in plain English."""
    from app.handlers.comments import router

    text, _ = router.ask_text(
        "Senior engineer. Explain clearly in plain English.",
        f"Explain this:\n{context[:2000]}",
        task="explain",
    )
    return f"## 💡 Explanation\n\n{text}"


def cmd_improve(context: str) -> str:
    """Suggest concrete, actionable improvements."""
    r, _ = safe_router_ask(
        "Staff engineer. Suggest concrete improvements. JSON only.",
        f"""Suggest improvements for:
{context[:2000]}

Return JSON:
{{
  "summary": "overall assessment",
  "improvements": [
    {{"area": "performance|security|readability|structure",
      "suggestion": "what to change",
      "example": "code example"}}
  ]
}}""",
        task="improve",
    )
    if not r or not r.get("improvements"):
        return "## ✨ Improvements\n\n_No improvements identified._"

    lines = [f"## ✨ Improvements\n\n**{r.get('summary', '')}**\n"]
    for i, imp in enumerate(r.get("improvements", [])[:4], 1):
        lines.append(f"### {i}. `{imp.get('area', '').upper()}` — {imp.get('suggestion', '')}")
        if imp.get("example"):
            lines.append(f"```\n{imp['example'][:300]}\n```")
    return "\n\n".join(lines)


def cmd_test(context: str) -> str:
    """Generate test cases with full pytest code."""
    r, _ = safe_router_ask(
        "Senior QA engineer. Generate tests. JSON only.",
        f"""Write tests for:
{context[:2000]}

Return JSON:
{{
  "framework": "pytest",
  "tests": [
    {{"name": "test_name", "type": "unit",
      "desc": "what it tests", "code": "full test code"}}
  ]
}}""",
        task="test_generation",
    )
    if not r or not r.get("tests"):
        return "## 🧪 Tests\n\n_Could not generate tests._"

    lines = [f"## 🧪 Tests ({r.get('framework', 'pytest')})\n"]
    for t in r.get("tests", [])[:3]:
        lines.append(
            f"### `{t.get('name', 'test')}` ({t.get('type', 'unit')})\n"
            f"*{t.get('desc', '')}*\n"
            f"```python\n{t.get('code', '')[:400]}\n```"
        )
    return "\n\n".join(lines)


def cmd_docs(context: str) -> str:
    """Generate docstring, usage example, and README section."""
    r, _ = safe_router_ask(
        "Technical writer. Generate documentation. JSON only.",
        f"""Generate docs for:
{context[:2000]}

Return JSON:
{{
  "docstring": "complete docstring",
  "usage": "usage example",
  "readme_section": "markdown section"
}}""",
        task="docs",
    )
    if not r or not r.get("docstring"):
        return "## 📚 Documentation\n\n_Could not generate docs._"

    return (
        f"## 📚 Documentation\n\n"
        f"**Docstring:**\n```\n{r.get('docstring', '')}\n```\n\n"
        f"**Usage:**\n```\n{r.get('usage', '')}\n```\n\n"
        f"**README section:**\n{r.get('readme_section', '')}"
    )


def cmd_refactor(context: str) -> str:
    """Suggest targeted refactoring with before/after examples."""
    r, _ = safe_router_ask(
        "Principal engineer. Suggest refactoring. JSON only.",
        f"""Suggest refactoring for:
{context[:2500]}

Return JSON:
{{
  "summary": "assessment",
  "refactors": [
    {{"type": "extract_function",
      "description": "what and why",
      "before": "snippet",
      "after": "refactored",
      "benefit": "benefit"}}
  ]
}}""",
        task="refactor",
    )
    if not r or not r.get("refactors"):
        return "## ♻️ Refactor\n\n_No refactoring opportunities identified._"

    lines = [f"## ♻️ Refactor\n\n**{r.get('summary', '')}**\n"]
    for i, ref in enumerate(r.get("refactors", [])[:4], 1):
        lines.append(f"### {i}. `{ref.get('type', '').upper()}` — {ref.get('description', '')}")
        if ref.get("before"):
            lines.append(f"**Before:**\n```\n{ref['before'][:300]}\n```")
        if ref.get("after"):
            lines.append(f"**After:**\n```\n{ref['after'][:300]}\n```")
        lines.append(f"✅ **Benefit:** {ref.get('benefit', '')}")
    return "\n\n".join(lines)


def cmd_gaps(context: str) -> str:
    """Identify test coverage gaps with risk ratings."""
    r, _ = safe_router_ask(
        "Senior QA engineer. Identify test gaps. JSON only.",
        f"""Analyze this code for test coverage gaps:
{context[:2500]}

Return JSON:
{{
  "coverage_assessment": "overall assessment",
  "gaps": [
    {{"area": "what is not tested",
      "risk": "high|medium|low",
      "suggested_test": "test to add"}}
  ]
}}""",
        task="gaps",
    )
    if not r or not r.get("gaps"):
        return "## 🔍 Test Coverage Gaps\n\n_No gaps identified._"

    lines = [f"## 🔍 Test Coverage Gaps\n\n**{r.get('coverage_assessment', '')}**\n"]
    for i, gap in enumerate(r.get("gaps", [])[:5], 1):
        lines.append(
            f"### {i}. {gap.get('area', '')} "
            f"— Risk: `{gap.get('risk', 'medium').upper()}`\n"
            f"**Suggested test:** {gap.get('suggested_test', '')}"
        )
    return "\n\n".join(lines)


def cmd_perf(context: str) -> str:
    """Analyze code for performance issues — complexity, memory, N+1."""
    r, _ = safe_router_ask(
        "Performance engineer. Analyze code for performance issues. JSON only.",
        f"""Analyze for performance problems:
{context[:2500]}

Return JSON:
{{
  "overall_rating": "fast|acceptable|slow|critical",
  "complexity_issues": [
    {{
      "location": "function or line",
      "current_complexity": "O(n²)",
      "issue": "what is slow",
      "fix": "optimized version",
      "improvement": "estimated speedup"
    }}
  ],
  "quick_wins": ["easy optimization 1"],
  "summary": "2 sentence overall assessment"
}}""",
        task="perf",
        max_tokens=1500,
    )
    if not r:
        return "## ⚡ Performance Analysis\n\n_Could not complete analysis._"

    rating = r.get("overall_rating", "acceptable")
    r_emoji = {"fast": "🟢", "acceptable": "🟡", "slow": "🟠", "critical": "🔴"}.get(rating, "🟡")

    issues_md = ""
    for i, issue in enumerate(r.get("complexity_issues", [])[:4], 1):
        issues_md += (
            f"\n### {i}. `{issue.get('location', '')}` "
            f"— {issue.get('current_complexity', '')}\n"
            f"**Problem:** {issue.get('issue', '')}\n\n"
            f"**Fix:**\n```python\n{issue.get('fix', '')[:400]}\n```\n"
            f"**Improvement:** {issue.get('improvement', '')}\n"
        )

    qw_md = "\n".join(f"- {w}" for w in r.get("quick_wins", [])[:5]) or "_No quick wins found._"
    return (
        f"## ⚡ Performance Analysis\n\n"
        f"**Rating:** {r_emoji} {rating.capitalize()}\n\n"
        f"**Summary:** {r.get('summary', '')}\n"
        f"{issues_md}\n"
        f"### 🎯 Quick Wins\n{qw_md}"
    )


def cmd_arch(repo: str, issue_number: int, issue: dict, token: str) -> str:
    """Architecture review — layers, coupling, god classes."""
    from app.handlers.comments import gh_get

    context = ""
    if "pull_request" in issue:
        try:
            files = gh_get(f"/repos/{repo}/pulls/{issue_number}/files", token)
            context = "Files changed:\n" + "\n".join(f["filename"] for f in files[:15])
        except Exception:
            pass

    if not context:
        context = f"Title: {issue.get('title', '')}\nBody: {(issue.get('body') or '')[:500]}"

    r, _ = safe_router_ask(
        "Software architect with 15+ years. Review architecture. JSON only.",
        f"""Review this for architectural issues:
{context}

Return JSON:
{{
  "health": "excellent|good|needs_work|critical",
  "violations": [
    {{
      "type": "layer_violation|circular_import|god_class|tight_coupling|other",
      "severity": "high|medium|low",
      "location": "file or module",
      "description": "what is wrong",
      "recommendation": "how to fix"
    }}
  ],
  "positive_patterns": ["good thing 1"],
  "refactoring_priority": "immediate|planned|backlog",
  "summary": "2 sentence assessment"
}}""",
        task="arch",
        max_tokens=1500,
    )
    if not r:
        return "## 🏗️ Architecture Review\n\n_Could not complete analysis._"

    health = r.get("health", "good")
    h_emoji = {"excellent": "🟢", "good": "🟡", "needs_work": "🟠", "critical": "🔴"}.get(
        health, "🟡"
    )

    violations_md = ""
    for v in r.get("violations", [])[:5]:
        sev = v.get("severity", "medium")
        s_em = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(sev, "🟡")
        violations_md += (
            f"\n- {s_em} **{v.get('type', '').replace('_', ' ').title()}** "
            f"— `{v.get('location', '')}`: {v.get('description', '')}\n"
            f"  → {v.get('recommendation', '')}"
        )

    pos_md = "\n".join(f"- ✅ {p}" for p in r.get("positive_patterns", [])[:3]) or ""
    priority = r.get("refactoring_priority", "planned")
    p_emoji = {"immediate": "🔴", "planned": "🟡", "backlog": "🟢"}.get(priority, "🟡")

    return (
        f"## 🏗️ Architecture Review\n\n"
        f"**Health:** {h_emoji} {health.replace('_', ' ').capitalize()}\n"
        f"**Refactoring Priority:** {p_emoji} {priority.capitalize()}\n\n"
        f"**Summary:** {r.get('summary', '')}\n"
        f"\n### Issues Found\n{violations_md or '_No violations found._'}\n"
        f"\n### ✅ Good Patterns\n{pos_md or '_None identified._'}"
    )
