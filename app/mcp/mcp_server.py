"""
app/mcp/server.py
GitHub Autopilot — MCP (Model Context Protocol) Server

Compatible with:
  Claude / Claude Code   — ~/.claude/mcp.json
  Cursor                 — .cursor/mcp.json
  Codex CLI              — ~/.codex/mcp.json
  OpenCode               — .opencode/mcp.json

Protocol: JSON-RPC 2.0 over HTTP POST /mcp
Auth:     Bearer token via MCP_API_KEY env var
          (leave unset during local development)
"""

import logging
import os
import time

log = logging.getLogger(__name__)

MCP_API_KEY = os.environ.get("MCP_API_KEY", "")

# ─── Tool Definitions ────────────────────────────────────────────────────────

MCP_TOOLS = [
    {
        "name": "analyze_pr",
        "description": (
            "Analyze a GitHub pull request for code quality, security risks, "
            "test coverage gaps, and blast radius. Returns grade (A-F), "
            "findings, and improvement suggestions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo":      {"type": "string", "description": "owner/repo format"},
                "pr_number": {"type": "integer", "description": "PR number"},
                "focus":     {"type": "string",
                              "enum": ["security", "performance", "quality", "all"],
                              "default": "all"},
            },
            "required": ["repo", "pr_number"],
        },
    },
    {
        "name": "fix_issue",
        "description": (
            "Get root cause analysis and a production-ready fix suggestion "
            "for a GitHub issue, with a verification test."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo":         {"type": "string"},
                "issue_number": {"type": "integer"},
                "context":      {"type": "string",
                                 "description": "Extra code context or stack trace"},
            },
            "required": ["repo", "issue_number"],
        },
    },
    {
        "name": "scan_secrets",
        "description": (
            "Scan a code snippet for exposed secrets and credentials. "
            "Uses 41 patterns with entropy gating."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content":  {"type": "string", "description": "Code to scan"},
                "filename": {"type": "string", "description": "Filename for context"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "explain_code",
        "description": "Get a plain-English explanation of a code snippet.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code":     {"type": "string"},
                "language": {"type": "string"},
                "depth":    {"type": "string",
                             "enum": ["brief", "standard", "deep"],
                             "default": "standard"},
            },
            "required": ["code"],
        },
    },
    {
        "name": "generate_tests",
        "description": "Generate a pytest test suite for a function or class.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code":          {"type": "string"},
                "framework":     {"type": "string",
                                  "enum": ["pytest", "unittest"],
                                  "default": "pytest"},
                "include_mocks": {"type": "boolean", "default": True},
            },
            "required": ["code"],
        },
    },
    {
        "name": "security_review",
        "description": "Security review of code or requirements.txt for CVEs and vulnerabilities.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content":      {"type": "string"},
                "content_type": {"type": "string",
                                 "enum": ["code", "requirements", "config"],
                                 "default": "code"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "get_repo_health",
        "description": "Get the health grade (A-F) for a repository with recommendations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/repo format"},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "run_command",
        "description": (
            "Run a read-only GitHub Autopilot slash command on an issue or PR. "
            "Available: /fix /explain /improve /refactor /perf /arch /impact "
            "/gaps /docs /test /security /summarize /budget /health "
            "/version /report /changelog"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo":         {"type": "string"},
                "issue_number": {"type": "integer"},
                "command":      {"type": "string",
                                 "description": "e.g. /fix, /explain, /security"},
                "context":      {"type": "string"},
                "installation_id": {"type": "integer",
                                    "description": "GitHub App installation ID"},
            },
            "required": ["repo", "issue_number", "command", "installation_id"],
        },
    },
]


# ─── Handlers ────────────────────────────────────────────────────────────────

def _handle_analyze_pr(args: dict) -> str:
    repo      = args.get("repo", "")
    pr_number = args.get("pr_number")
    focus     = args.get("focus", "all")

    if not repo or not pr_number:
        return "Error: repo and pr_number are required."

    try:
        from app.ai.router import router
        from app.github.auth import get_installation_token
        from app.github.client import gh_get

        install_id = args.get("installation_id")
        if not install_id:
            return "Error: installation_id is required for GitHub API access."

        token = get_installation_token(install_id)
        pr    = gh_get(f"/repos/{repo}/pulls/{pr_number}", token)

        result, _meta = router.ask(
            "Senior code reviewer. Return JSON only.",
            f"""Analyze PR #{pr_number} '{pr.get('title','')}' in {repo}.
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
            f"**Grade:** {result.get('grade','N/A')}",
            f"**Summary:** {result.get('summary','')}",
            f"**Recommendation:** {result.get('recommendation','')}",
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
    repo         = args.get("repo", "")
    issue_number = args.get("issue_number")
    context      = args.get("context", "")
    install_id   = args.get("installation_id")

    if not repo or not issue_number:
        return "Error: repo and issue_number are required."
    if not install_id:
        return "Error: installation_id is required."

    try:
        from app.ai.router import router
        from app.github.auth import get_installation_token
        from app.github.client import gh_get

        token = get_installation_token(install_id)
        issue = gh_get(f"/repos/{repo}/issues/{issue_number}", token)
        title = issue.get("title", "")
        body  = (issue.get("body") or "")[:1000]

        result, _meta = router.ask(
            "Senior engineer. Return JSON only.",
            f"""Issue #{issue_number}: {title}
{body}
Context: {context[:500] if context else 'none'}

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

        return "\n".join([
            f"## Fix for Issue #{issue_number}",
            "",
            f"**Root Cause:** {result.get('root_cause','')}",
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
            f"*Confidence: {int(float(result.get('confidence',0.8))*100)}%*",
        ])

    except Exception as e:
        log.error(f"mcp.fix_issue error: {e}")
        return f"Error: {str(e)[:200]}"


def _handle_scan_secrets(args: dict) -> str:
    content  = args.get("content", "")
    filename = args.get("filename", "unknown")

    if not content:
        return "Error: content is required."

    try:
        from app.security.enhanced_secrets import scan_diff, format_findings

        # scan_diff reads lines starting with "+" (git diff format).
        # Prefix each line so raw content is fully scanned.
        diff_text = "\n".join(f"+{line}" for line in content.splitlines())
        findings  = scan_diff(diff_text, filename)

        if not findings:
            return "✅ No secrets detected."

        return format_findings(findings, repo=filename or "mcp-scan")

    except Exception as e:
        log.error(f"mcp.scan_secrets error: {e}")
        return f"Error: {str(e)[:200]}"


def _handle_explain_code(args: dict) -> str:
    code     = args.get("code", "")
    language = args.get("language", "")
    depth    = args.get("depth", "standard")

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
    code          = args.get("code", "")
    framework     = args.get("framework", "pytest")
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
    content      = args.get("content", "")
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

        risk     = result.get("risk_level", "unknown").upper()
        findings = result.get("findings", [])
        cves     = result.get("cve_risks", [])

        lines = ["## Security Review", f"**Risk Level:** {risk}", ""]
        if findings:
            lines += ["**Findings:**"]
            for f in findings[:8]:
                sev = f.get("severity", "").upper()
                lines.append(f"- [{sev}] {f.get('issue','')} — {f.get('fix','')}")
        if cves:
            lines += ["", "**CVE Risks:**"] + [f"- {c}" for c in cves]
        return "\n".join(lines)

    except Exception as e:
        log.error(f"mcp.security_review error: {e}")
        return f"Error: {str(e)[:200]}"


def _handle_get_repo_health(args: dict) -> str:
    repo       = args.get("repo", "")
    install_id = args.get("installation_id")

    if not repo:
        return "Error: repo is required."
    if not install_id:
        return "Error: installation_id is required."

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

        grade  = result.get("grade", "N/A")
        score  = result.get("score", 0)
        issues = result.get("top_issues", [])
        wins   = result.get("quick_wins", [])

        lines = [f"## Repository Health — {repo}", "",
                 f"**Grade:** {grade} ({score}/10)", ""]
        if issues:
            lines += ["**Top Issues:**"] + [f"- {i}" for i in issues] + [""]
        if wins:
            lines += ["**Quick Wins:**"] + [f"- {w}" for w in wins]
        return "\n".join(lines)

    except Exception as e:
        log.error(f"mcp.get_repo_health error: {e}")
        return f"Error: {str(e)[:200]}"


def _handle_run_command(args: dict) -> str:
    repo         = args.get("repo", "")
    issue_number = args.get("issue_number")
    command      = args.get("command", "").strip()
    context      = args.get("context", "")
    install_id   = args.get("installation_id")

    if not repo or not issue_number or not command or not install_id:
        return "Error: repo, issue_number, command, and installation_id are required."

    # Read-only commands only — destructive ones require GitHub comment for audit trail
    ALLOWED = {
        "/fix", "/explain", "/improve", "/refactor", "/perf",
        "/arch", "/impact", "/gaps", "/docs", "/test",
        "/security", "/summarize", "/budget", "/health",
        "/version", "/report", "/changelog",
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
        body  = (issue.get("body") or "")[:2000]
        full_context = f"{body}\n\n{context}".strip() if context else body

        parsed_cmd = _extract_command(f"{cmd} {context}".strip())
        if not parsed_cmd:
            return f"Error: could not parse command '{command}'"

        # Signatures verified against comments.py
        handler_map = {
            "/fix":       lambda: ch._cmd_fix(title, full_context),
            "/explain":   lambda: ch._cmd_explain(full_context),
            "/improve":   lambda: ch._cmd_improve(full_context),
            "/refactor":  lambda: ch._cmd_refactor(full_context),
            "/perf":      lambda: ch._cmd_perf(full_context),
            "/gaps":      lambda: ch._cmd_gaps(full_context),
            "/docs":      lambda: ch._cmd_docs(full_context),
            "/test":      lambda: ch._cmd_test(full_context),
            "/arch":      lambda: ch._cmd_arch(repo, issue_number, issue, token),
            "/impact":    lambda: ch._cmd_impact(repo, issue_number, issue, token),
            "/summarize": lambda: ch._cmd_summarize(repo, issue_number, token),
            "/security":  lambda: ch._cmd_security(repo, issue_number, issue, token),
            "/changelog": lambda: ch._cmd_changelog(repo, token),
            "/health":    lambda: ch._cmd_health(repo, token),
            "/version":   lambda: ch._cmd_version(repo, token),
            "/report":    lambda: ch._cmd_report(repo),
            "/budget":    lambda: ch._cmd_budget(),
        }

        handler = handler_map.get(parsed_cmd)
        if handler:
            return handler()
        return f"Command {parsed_cmd} is allowed but not yet wired via MCP."

    except Exception as e:
        log.error(f"mcp.run_command error: {e}")
        return f"Error: {str(e)[:200]}"


# ─── Dispatch ────────────────────────────────────────────────────────────────

TOOL_HANDLERS = {
    "analyze_pr":     _handle_analyze_pr,
    "fix_issue":      _handle_fix_issue,
    "scan_secrets":   _handle_scan_secrets,
    "explain_code":   _handle_explain_code,
    "generate_tests": _handle_generate_tests,
    "security_review":_handle_security_review,
    "get_repo_health":_handle_get_repo_health,
    "run_command":    _handle_run_command,
}


def handle_mcp_request(
    method: str, params: dict, auth_token: str
) -> tuple[dict, int]:
    """
    Main MCP request handler. Called from server.py /mcp endpoint.
    Returns (response_dict, http_status_code).
    """
    if MCP_API_KEY and auth_token != MCP_API_KEY:
        return {"error": {"code": -32001, "message": "Unauthorized"}}, 401

    start = time.time()

    if method == "tools/list":
        return {"tools": MCP_TOOLS}, 200

    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})

        handler = TOOL_HANDLERS.get(tool_name)
        if not handler:
            return {
                "error": {
                    "code": -32602,
                    "message": f"Unknown tool: {tool_name}",
                    "available": list(TOOL_HANDLERS.keys()),
                }
            }, 400

        try:
            result_text = handler(tool_args)
            latency_ms  = int((time.time() - start) * 1000)
            log.info(f"mcp.tool_call tool={tool_name} latency={latency_ms}ms")
            return {
                "content":    [{"type": "text", "text": result_text}],
                "latency_ms": latency_ms,
            }, 200
        except Exception as e:
            log.error(f"mcp.tool_call error tool={tool_name}: {e}")
            return {"error": {"code": -32000, "message": str(e)[:200]}}, 500

    if method == "initialize":
        return {
            "protocolVersion": "2024-11-05",
            "capabilities":    {"tools": {"listChanged": False}},
            "serverInfo":      {"name": "github-autopilot", "version": "4.2.0"},
        }, 200

    return {"error": {"code": -32601, "message": f"Unknown method: {method}"}}, 400
