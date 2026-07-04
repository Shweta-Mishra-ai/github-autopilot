"""app/mcp/tools.py — MCP tool schema definitions (data only).

Split out of mcp_server.py so the tool catalog lives apart from transport
and handler logic. Imported as MCP_TOOLS by the server and re-exported there
for backward compatibility.
"""

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
                "repo": {"type": "string", "description": "owner/repo format"},
                "pr_number": {"type": "integer", "description": "PR number"},
                "focus": {
                    "type": "string",
                    "enum": ["security", "performance", "quality", "all"],
                    "default": "all",
                },
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
                "repo": {"type": "string"},
                "issue_number": {"type": "integer"},
                "context": {"type": "string", "description": "Extra code context or stack trace"},
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
        "description": "Security review of code or requirements.txt for CVEs and vulnerabilities.",
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
                "repo": {"type": "string"},
                "issue_number": {"type": "integer"},
                "command": {"type": "string", "description": "e.g. /fix, /explain, /security"},
                "context": {"type": "string"},
                "installation_id": {"type": "integer", "description": "GitHub App installation ID"},
            },
            "required": ["repo", "issue_number", "command", "installation_id"],
        },
    },
]
