"""
app/mcp/server.py
GitHub Autopilot — MCP (Model Context Protocol) Server

Compatible with:
  Claude / Claude Code   — ~/.claude/mcp.json
  Cursor                 — .cursor/mcp.json
  Codex CLI              — ~/.codex/mcp.json
  OpenCode               — .opencode/mcp.json

Protocol: JSON-RPC 2.0 over HTTP POST /mcp
Auth:     Bearer token via MCP_API_KEY env var, read at request time.
          FAIL CLOSED — if MCP_API_KEY is unset the server rejects every
          request with 503. A constant-time compare guards the token.
Tenant:   MCP_ALLOWED_INSTALLATIONS (optional) restricts which GitHub App
          installation IDs the tools may act on.
"""

import hmac
import logging
import os
import time

from app import __version__
from app.mcp.tools import MCP_TOOLS
from app.mcp.handlers import (
    TOOL_HANDLERS,
    _installation_allowed,
    _handle_analyze_pr,
    _handle_fix_issue,
    _handle_scan_secrets,
    _handle_explain_code,
    _handle_generate_tests,
    _handle_security_review,
    _handle_get_repo_health,
    _handle_run_command,
)

# Re-exported above for backward compatibility: existing imports and tests use
# app.mcp.mcp_server.{MCP_TOOLS, TOOL_HANDLERS, _handle_*, _installation_allowed}.
# Listing them in __all__ marks the imports as intentional public re-exports.
__all__ = [
    "MCP_TOOLS",
    "TOOL_HANDLERS",
    "handle_mcp_request",
    "_installation_allowed",
    "_handle_analyze_pr",
    "_handle_fix_issue",
    "_handle_scan_secrets",
    "_handle_explain_code",
    "_handle_generate_tests",
    "_handle_security_review",
    "_handle_get_repo_health",
    "_handle_run_command",
]

log = logging.getLogger(__name__)


def _mcp_api_key() -> str:
    """Read MCP_API_KEY from env at request time (supports zero-downtime rotation)."""
    return os.environ.get("MCP_API_KEY", "")


def handle_mcp_request(method: str, params: dict, auth_token: str) -> tuple[dict, int]:
    """
    Main MCP request handler. Called from server.py /mcp endpoint.
    Returns (response_dict, http_status_code).
    """
    # FAIL CLOSED: no configured key → refuse everything (503, not open).
    _mcp_key = _mcp_api_key()
    if not _mcp_key:
        log.error("mcp.no_api_key — refusing request (fail closed). Set MCP_API_KEY.")
        return {"error": {"code": -32001, "message": "Server auth not configured"}}, 503
    # Constant-time compare to avoid token-timing side channel.
    if not hmac.compare_digest(auth_token or "", _mcp_key):
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
            latency_ms = int((time.time() - start) * 1000)
            log.info(f"mcp.tool_call tool={tool_name} latency={latency_ms}ms")
            return {
                "content": [{"type": "text", "text": result_text}],
                "latency_ms": latency_ms,
            }, 200
        except Exception as e:
            log.error(f"mcp.tool_call error tool={tool_name}: {e}")
            return {"error": {"code": -32000, "message": str(e)[:200]}}, 500

    if method == "initialize":
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "github-autopilot", "version": __version__},
        }, 200

    return {"error": {"code": -32601, "message": f"Unknown method: {method}"}}, 400
