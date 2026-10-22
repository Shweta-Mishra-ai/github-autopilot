"""app/mcp/tools.py — MCP tool schema definitions (data only).

Split out of mcp_server.py so the tool catalog lives apart from transport
and handler logic. Imported as MCP_TOOLS by the server and re-exported there
for backward compatibility.
"""

from app.security.enhanced_secrets import PATTERNS as _SECRET_PATTERNS

# Every tool that reads GitHub needs an installation token, and the handlers
# refuse a call without one. The schemas for analyze_pr, fix_issue and
# get_repo_health did not declare it, so an MCP client following the schema
# could never make a call that succeeded.
_INSTALLATION_ID = {"type": "integer", "description": "GitHub App installation ID"}

MCP_TOOLS = [
    {
        "name": "analyze_pr",
        "description": (
            "Analyze a GitHub pull request's diff: a summary, security issues, "
            "test gaps, blast radius and improvements the model found, and an "
            "approve/request_changes recommendation. No grade or score."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/repo format"},
                "pr_number": {"type": "integer", "description": "PR number"},
                "focus": {
                    "type": "string",
                    "enum": ["security", "performance", "quality", "all"],
                    "default": "all",
                },
                "installation_id": _INSTALLATION_ID,
            },
            "required": ["repo", "pr_number", "installation_id"],
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
                "repo": {"type": "string"},
                "issue_number": {"type": "integer"},
                "context": {"type": "string", "description": "Extra code context or stack trace"},
                "installation_id": _INSTALLATION_ID,
            },
            "required": ["repo", "issue_number", "installation_id"],
        },
    },
    {
        "name": "scan_secrets",
        "description": (
            "Scan a code snippet for exposed secrets and credentials. "
            # Counted, not written down: the hand-typed "41" was already wrong.
            f"Uses {len(_SECRET_PATTERNS)} patterns with entropy gating. Files "
            "named as examples or templates (e.g. .env.example) are not scanned."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Code to scan"},
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
                "code": {"type": "string"},
                "language": {"type": "string"},
                "depth": {
                    "type": "string",
                    "enum": ["brief", "standard", "deep"],
                    "default": "standard",
                },
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
                "code": {"type": "string"},
                "framework": {
                    "type": "string",
                    "enum": ["pytest", "unittest"],
                    "default": "pytest",
                },
                "include_mocks": {"type": "boolean", "default": True},
            },
            "required": ["code"],
        },
    },
    {
        "name": "security_review",
        "description": (
            "Model review of code or config for security weaknesses. Not an "
            "advisory lookup: it names no CVEs. Use /security on a PR for "
            "GitHub's advisory data on pinned dependencies."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "content_type": {
                    "type": "string",
                    "enum": ["code", "requirements", "config"],
                    "default": "code",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "get_repo_health",
        "description": (
            "Repository health checks (open issues, open PRs, license, "
            "description) with a recommendation for each one flagged. No grade."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/repo format"},
                "installation_id": _INSTALLATION_ID,
            },
            "required": ["repo", "installation_id"],
        },
    },
    {
        "name": "codebase_map",
        "description": (
            "Structural map of the codebase, derived from the AST: which modules "
            "exist, what imports what, import cycles, modules nothing imports "
            "(dead-code candidates), and the most depended-on files. Pass "
            "'module' to get the dependants of one module instead of the whole "
            "map. Read-only and local — no GitHub token needed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "module": {
                    "type": "string",
                    "description": (
                        "Dotted module path, e.g. app.core.config. Omit for the "
                        "whole-codebase summary."
                    ),
                },
                "targets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Paths to scan. Default: app, server.py, worker.py",
                },
                "entrypoints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Modules expected to have no importers, so they are not "
                        "reported as dead code."
                    ),
                },
            },
            "required": [],
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
                "repo": {"type": "string"},
                "issue_number": {"type": "integer"},
                "command": {"type": "string", "description": "e.g. /fix, /explain, /security"},
                "context": {"type": "string"},
                "installation_id": _INSTALLATION_ID,
            },
            "required": ["repo", "issue_number", "command", "installation_id"],
        },
    },
]
