"""app/mcp/handlers.py — MCP tool handler implementations + registry.

Split out of mcp_server.py to keep the transport/dispatch layer small. Each
_handle_* returns a Markdown/text string and never raises; TOOL_HANDLERS maps
tool name -> handler. _installation_allowed() lives here since only handlers
use it.
"""

import logging
import os

log = logging.getLogger(__name__)


def _installation_allowed(install_id) -> bool:
    """
    Optional tenant isolation for install-id-scoped tools.

    MCP_ALLOWED_INSTALLATIONS is a comma-separated allowlist of GitHub App
    installation IDs. When set, MCP tools may only act on those installations —
    a single leaked MCP key can no longer drive commands against every install
    the App is on. When unset, all installations are allowed (single-tenant
    default) and a warning is logged so operators know the lever exists.
    """
    raw = os.environ.get("MCP_ALLOWED_INSTALLATIONS", "").strip()
    if not raw:
        log.warning(
            "mcp.installation_allowlist_unset — any installation_id accepted. "
            "Set MCP_ALLOWED_INSTALLATIONS to restrict (recommended for multi-tenant)."
        )
        return True
    allowed = {p.strip() for p in raw.split(",") if p.strip()}
    return str(install_id) in allowed


# ─── Tool Definitions ────────────────────────────────────────────────────────


# ─── Handlers ────────────────────────────────────────────────────────────────


def _handle_analyze_pr(args: dict) -> str:
    repo = args.get("repo", "")
    pr_number = args.get("pr_number")
    focus = args.get("focus", "all")

    if not repo or not pr_number:
        return "Error: repo and pr_number are required."

    try:
        from app.ai.router import router
        from app.github.auth import get_installation_token
        from app.github.client import gh_get

        install_id = args.get("installation_id")
        if not install_id:
            return "Error: installation_id is required for GitHub API access."
        if not _installation_allowed(install_id):
            return "Error: installation_id not permitted (MCP_ALLOWED_INSTALLATIONS)."

        token = get_installation_token(install_id)
        pr = gh_get(f"/repos/{repo}/pulls/{pr_number}", token)

        result, _meta = router.ask(
            "Senior code reviewer. Return JSON only.",
            f"""Analyze PR #{pr_number} '{pr.get("title", "")}' in {repo}.
Focus: {focus}

Return JSON:
{{
  "grade": "B+",
  "summary": "one sentence",
  "quality_score": 7.5,
  "security_issues": [],
  "test_gaps": [],
  "blast_radius": [],
  "improvements": [],
  "recommendation": "approve|request_changes"
}}""",
            task="pr_analysis",
            max_tokens=1000,
        )

        lines = [
            f"## PR #{pr_number} Analysis — {repo}",
            "",
            f"**Grade:** {result.get('grade', 'N/A')}",
            f"**Summary:** {result.get('summary', '')}",
            f"**Recommendation:** {result.get('recommendation', '')}",
            "",
        ]
        if result.get("security_issues"):
            lines += ["**Security:**"] + [f"- {i}" for i in result["security_issues"]] + [""]
        if result.get("test_gaps"):
            lines += ["**Test Gaps:**"] + [f"- {g}" for g in result["test_gaps"]] + [""]
        if result.get("improvements"):
            lines += ["**Improvements:**"] + [f"- {i}" for i in result["improvements"]]
        return "\n".join(lines)

    except Exception as e:
        log.error(f"mcp.analyze_pr error: {e}")
        return f"Error: {str(e)[:200]}"


def _handle_fix_issue(args: dict) -> str:
    repo = args.get("repo", "")
    issue_number = args.get("issue_number")
    context = args.get("context", "")
    install_id = args.get("installation_id")

    if not repo or not issue_number:
        return "Error: repo and issue_number are required."
    if not install_id:
        return "Error: installation_id is required."
    if not _installation_allowed(install_id):
        return "Error: installation_id not permitted (MCP_ALLOWED_INSTALLATIONS)."

    try:
        from app.ai.router import router
        from app.github.auth import get_installation_token
        from app.github.client import gh_get

        token = get_installation_token(install_id)
        issue = gh_get(f"/repos/{repo}/issues/{issue_number}", token)
        title = issue.get("title", "")
        body = (issue.get("body") or "")[:1000]

        result, _meta = router.ask(
            "Senior engineer. Return JSON only.",
            f"""Issue #{issue_number}: {title}
{body}
Context: {context[:500] if context else "none"}

Return JSON:
{{
  "root_cause": "...",
  "fix": "code here",
  "test": "pytest test here",
  "confidence": 0.85
}}""",
            task="fix_command",
            max_tokens=1500,
        )

        return "\n".join(
            [
                f"## Fix for Issue #{issue_number}",
                "",
                f"**Root Cause:** {result.get('root_cause', '')}",
                "",
                "**Fix:**",
                "```python",
                result.get("fix", ""),
                "```",
                "",
                "**Verification Test:**",
                "```python",
                result.get("test", ""),
                "```",
                "",
                f"*Confidence: {int(float(result.get('confidence', 0.8)) * 100)}%*",
            ]
        )

    except Exception as e:
        log.error(f"mcp.fix_issue error: {e}")
        return f"Error: {str(e)[:200]}"


def _handle_scan_secrets(args: dict) -> str:
    content = args.get("content", "")
    filename = args.get("filename", "unknown")

    if not content:
        return "Error: content is required."

    try:
        from app.security.enhanced_secrets import scan_diff, format_findings

        # scan_diff reads lines starting with "+" (git diff format).
        # Prefix each line so raw content is fully scanned.
        diff_text = "\n".join(f"+{line}" for line in content.splitlines())
        findings = scan_diff(diff_text, filename)

        if not findings:
            return "✅ No secrets detected."

        return format_findings(findings, repo=filename or "mcp-scan")

    except Exception as e:
        log.error(f"mcp.scan_secrets error: {e}")
        return f"Error: {str(e)[:200]}"


def _handle_explain_code(args: dict) -> str:
    code = args.get("code", "")
    language = args.get("language", "")
    depth = args.get("depth", "standard")

    if not code:
        return "Error: code is required."

    try:
        from app.ai.router import router

        max_tokens = {"brief": 400, "standard": 800, "deep": 1500}.get(depth, 800)

        result, _meta = router.ask_text(
            "Expert teacher. Explain code clearly.",
            f"Explain this {language} code:\n\n```\n{code[:4000]}\n```\n\nDepth: {depth}",
            task="explain",
            max_tokens=max_tokens,
        )
        return result

    except Exception as e:
        log.error(f"mcp.explain_code error: {e}")
        return f"Error: {str(e)[:200]}"


def _handle_generate_tests(args: dict) -> str:
    code = args.get("code", "")
    framework = args.get("framework", "pytest")
    include_mocks = args.get("include_mocks", True)

    if not code:
        return "Error: code is required."

    try:
        from app.ai.router import router

        result, _meta = router.ask_text(
            f"Expert {framework} test writer. Return only test code.",
            f"Generate {framework} tests for:\n\n```python\n{code[:4000]}\n```"
            + ("\n\nInclude mocks for external dependencies." if include_mocks else ""),
            task="test_generation",
            max_tokens=2000,
        )
        return result

    except Exception as e:
        log.error(f"mcp.generate_tests error: {e}")
        return f"Error: {str(e)[:200]}"


def _handle_security_review(args: dict) -> str:
    content = args.get("content", "")
    content_type = args.get("content_type", "code")

    if not content:
        return "Error: content is required."

    try:
        from app.ai.router import router

        result, _meta = router.ask(
            "Security expert. Return JSON only.",
            f"""Security review of {content_type}:

```
{content[:4000]}
```

Return JSON:
{{
  "risk_level": "low|medium|high|critical",
  "findings": [{{"issue":"","severity":"","line":0,"fix":""}}],
  "cve_risks": [],
  "summary": ""
}}""",
            task="security_report",
            max_tokens=1200,
        )

        risk = result.get("risk_level", "unknown").upper()
        findings = result.get("findings", [])
        cves = result.get("cve_risks", [])

        lines = ["## Security Review", f"**Risk Level:** {risk}", ""]
        if findings:
            lines += ["**Findings:**"]
            for f in findings[:8]:
                sev = f.get("severity", "").upper()
                lines.append(f"- [{sev}] {f.get('issue', '')} — {f.get('fix', '')}")
        if cves:
            lines += ["", "**CVE Risks:**"] + [f"- {c}" for c in cves]
        return "\n".join(lines)

    except Exception as e:
        log.error(f"mcp.security_review error: {e}")
        return f"Error: {str(e)[:200]}"


def _handle_get_repo_health(args: dict) -> str:
    repo = args.get("repo", "")
    install_id = args.get("installation_id")

    if not repo:
        return "Error: repo is required."
    if not install_id:
        return "Error: installation_id is required."
    if not _installation_allowed(install_id):
        return "Error: installation_id not permitted (MCP_ALLOWED_INSTALLATIONS)."

    try:
        from app.ai.router import router

        result, _meta = router.ask(
            "DevOps expert. Return JSON only.",
            f"""Grade repository health for {repo}.

Return JSON:
{{
  "grade": "B",
  "score": 7.5,
  "dimensions": {{"ci_cd":8,"test_coverage":7,"security":8,"docs":6,"deps":9}},
  "top_issues": [],
  "quick_wins": []
}}""",
            task="standard",
            max_tokens=800,
        )

        grade = result.get("grade", "N/A")
        score = result.get("score", 0)
        issues = result.get("top_issues", [])
        wins = result.get("quick_wins", [])

        lines = [f"## Repository Health — {repo}", "", f"**Grade:** {grade} ({score}/10)", ""]
        if issues:
            lines += ["**Top Issues:**"] + [f"- {i}" for i in issues] + [""]
        if wins:
            lines += ["**Quick Wins:**"] + [f"- {w}" for w in wins]
        return "\n".join(lines)

    except Exception as e:
        log.error(f"mcp.get_repo_health error: {e}")
        return f"Error: {str(e)[:200]}"


def _handle_run_command(args: dict) -> str:
    repo = args.get("repo", "")
    issue_number = args.get("issue_number")
    command = args.get("command", "").strip()
    context = args.get("context", "")
    install_id = args.get("installation_id")

    if not repo or not issue_number or not command or not install_id:
        return "Error: repo, issue_number, command, and installation_id are required."
    if not _installation_allowed(install_id):
        return "Error: installation_id not permitted (MCP_ALLOWED_INSTALLATIONS)."

    # Read-only commands only — destructive ones require GitHub comment for audit trail
    ALLOWED = {
        "/fix",
        "/explain",
        "/improve",
        "/refactor",
        "/perf",
        "/arch",
        "/impact",
        "/gaps",
        "/docs",
        "/test",
        "/security",
        "/summarize",
        "/budget",
        "/health",
        "/version",
        "/report",
        "/changelog",
    }

    cmd = command.split()[0].lower()
    if cmd not in ALLOWED:
        return (
            f"Error: '{cmd}' is not available via MCP. "
            "Destructive commands (/merge /autofix /apply /release "
            "/rollback /runtests) require a direct GitHub comment for safety."
        )

    try:
        from app.github.auth import get_installation_token
        from app.github.client import gh_get
        from app.handlers.comments import _extract_command
        import app.handlers.comments as ch

        token = get_installation_token(install_id)
        issue = gh_get(f"/repos/{repo}/issues/{issue_number}", token)
        title = issue.get("title", "")
        body = (issue.get("body") or "")[:2000]
        full_context = f"{body}\n\n{context}".strip() if context else body

        parsed_cmd = _extract_command(f"{cmd} {context}".strip())
        if not parsed_cmd:
            return f"Error: could not parse command '{command}'"

        # Signatures verified against comments.py
        handler_map = {
            "/fix": lambda: ch._cmd_fix(title, full_context),
            "/explain": lambda: ch._cmd_explain(full_context),
            "/improve": lambda: ch._cmd_improve(full_context),
            "/refactor": lambda: ch._cmd_refactor(full_context),
            "/perf": lambda: ch._cmd_perf(full_context),
            "/gaps": lambda: ch._cmd_gaps(full_context),
            "/docs": lambda: ch._cmd_docs(full_context),
            "/test": lambda: ch._cmd_test(full_context),
            "/arch": lambda: ch._cmd_arch(repo, issue_number, issue, token),
            "/impact": lambda: ch._cmd_impact(repo, issue_number, issue, token),
            "/summarize": lambda: ch._cmd_summarize(repo, issue_number, token),
            "/security": lambda: ch._cmd_security(repo, issue_number, issue, token),
            "/changelog": lambda: ch._cmd_changelog(repo, token),
            "/health": lambda: ch._cmd_health(repo, token),
            "/version": lambda: ch._cmd_version(repo, token),
            "/report": lambda: ch._cmd_report(repo),
            "/budget": lambda: ch._cmd_budget(),
        }

        handler = handler_map.get(parsed_cmd)
        if handler:
            return handler()
        return f"Command {parsed_cmd} is allowed but not yet wired via MCP."

    except Exception as e:
        log.error(f"mcp.run_command error: {e}")
        return f"Error: {str(e)[:200]}"


def _handle_codebase_map(args: dict) -> str:
    """
    Structural map of the local codebase, derived from the AST.

    Local-only and read-only: it needs no GitHub token and no installation_id
    because it analyses the deployed source tree, not a remote repository. It
    never imports what it reads, so pointing it at untrusted code executes
    nothing.
    """
    from app.intelligence.codegraph import build_graph

    focus = (args.get("module") or "").strip()
    targets = args.get("targets") or ["app", "server.py", "worker.py"]

    try:
        graph = build_graph(*targets, root=args.get("root") or ".")
    except Exception as e:
        log.error(f"mcp.codebase_map error: {e}")
        return f"Error: {str(e)[:200]}"

    if not graph.nodes:
        return "No Python modules found. Check the `targets` argument."

    if focus:
        node = graph.nodes.get(focus)
        if node is None:
            near = [n for n in sorted(graph.nodes) if focus in n][:8]
            hint = ("\n\nDid you mean:\n" + "\n".join(f"- `{n}`" for n in near)) if near else ""
            return f"No module `{focus}` in the graph.{hint}"

        importers = sorted(e.source for e in graph.edges if e.target == focus)
        imports = sorted(e.target for e in graph.edges if e.source == focus)
        lines = [
            f"## `{focus}`",
            "",
            f"**Path:** `{node.path}` · **Layer:** {node.layer} · **Lines:** {node.loc}",
            f"**Functions:** {node.functions} · **Classes:** {node.classes}",
            "",
            f"### Imported by ({len(importers)})",
            *([f"- `{m}`" for m in importers] or ["_nothing — this module may be dead code_"]),
            "",
            f"### Imports ({len(imports)})",
            *([f"- `{m}`" for m in imports] or ["_nothing internal_"]),
        ]
        if node.external_deps:
            lines += ["", "### External", ", ".join(f"`{d}`" for d in node.external_deps)]
        return "\n".join(lines)

    stats = graph.to_dict()["stats"]
    entrypoints = tuple(args.get("entrypoints") or ("app", "server", "worker"))
    orphans = graph.orphans(entrypoints)

    lines = [
        "## Codebase map",
        "",
        f"**{stats['modules']}** modules · **{stats['edges']}** internal imports · "
        f"**{stats['total_loc']:,}** lines",
        "",
        "### Most depended-on",
        "",
        "| Module | Lines | Imported by | Imports |",
        "|---|---:|---:|---:|",
    ]
    lines += [
        f"| `{h['id']}` | {h['loc']} | {h['fan_in']} | {h['fan_out']} |"
        for h in stats["hotspots"][:10]
    ]

    if stats["cycles"]:
        lines += ["", "### Import cycles", ""]
        lines += [f"- `{' → '.join(c)}`" for c in stats["cycles"]]
    else:
        lines += ["", "### Import cycles", "", "None."]

    if orphans:
        lines += ["", f"### Imported by nothing ({len(orphans)})", ""]
        lines += [f"- `{o}`" for o in orphans]

    lines += ["", "_Pass `module` for the dependants of a single module._"]
    return "\n".join(lines)


TOOL_HANDLERS = {
    "analyze_pr": _handle_analyze_pr,
    "codebase_map": _handle_codebase_map,
    "fix_issue": _handle_fix_issue,
    "scan_secrets": _handle_scan_secrets,
    "explain_code": _handle_explain_code,
    "generate_tests": _handle_generate_tests,
    "security_review": _handle_security_review,
    "get_repo_health": _handle_get_repo_health,
    "run_command": _handle_run_command,
}
