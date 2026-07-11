"""
app/mcp/server.py
GitHub Autopilot — MCP (Model Context Protocol) Server

Compatible with:
  Claude / Claude Code   — ~/.claude/mcp.json
  Cursor                 — .cursor/mcp.json
  Codex CLI              — ~/.codex/mcp.json
  OpenCode               — .opencode/mcp.json

Protocol: JSON-RPC 2.0 over HTTP POST /mcp
Auth:     Bearer token, read at request time (zero-downtime rotation).
          Two forms, combinable:
            MCP_API_KEY   — single key, client label "default" (legacy)
            MCP_API_KEYS  — comma-separated name:key pairs, e.g.
                            "laptop:tok1,ci:tok2" — enables per-client
                            revocation and an attributable audit log.
          FAIL CLOSED — if neither is set the server rejects every request
          with 503. Constant-time compares guard every candidate token.
Audit:    every tools/call is logged as mcp.audit with the client label and
          tool name (never the arguments — they can contain source code).
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


def _mcp_named_keys() -> dict[str, str]:
    """
    Parse MCP_API_KEYS ("name:key,name2:key2") into {name: key}.
    Malformed entries (no colon, empty name/key) are skipped with a warning —
    a typo must not silently disable a *different*, valid key.
    """
    raw = os.environ.get("MCP_API_KEYS", "")
    keys: dict[str, str] = {}
    for entry in filter(None, (e.strip() for e in raw.split(","))):
        name, sep, key = entry.partition(":")
        if not sep or not name.strip() or not key.strip():
            log.warning(f"mcp.bad_key_entry skipped (want name:key): {entry[:20]!r}...")
            continue
        keys[name.strip()] = key.strip()
    return keys


def _resolve_client(auth_token: str) -> str | None:
    """
    Return the client label for a valid token, else None.
    Compares against EVERY candidate (no early exit) so response timing does
    not reveal which key position matched.
    """
    token = auth_token or ""
    matched: str | None = None
    legacy = _mcp_api_key()
    if legacy and hmac.compare_digest(token, legacy):
        matched = "default"
    for name, key in _mcp_named_keys().items():
        if hmac.compare_digest(token, key) and matched is None:
            matched = name
    return matched


def handle_mcp_request(method: str, params: dict, auth_token: str) -> tuple[dict, int]:
    """
    Main MCP request handler. Called from server.py /mcp endpoint.
    Returns (response_dict, http_status_code).
    """
    # FAIL CLOSED: no configured key → refuse everything (503, not open).
    if not _mcp_api_key() and not _mcp_named_keys():
        log.error(
            "mcp.no_api_key — refusing request (fail closed). Set MCP_API_KEY or MCP_API_KEYS."
        )
        return {"error": {"code": -32001, "message": "Server auth not configured"}}, 503
    client = _resolve_client(auth_token)
    if client is None:
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
            # Audit line: WHO did WHAT — never the arguments (may contain code).
            log.info(f"mcp.audit client={client} tool={tool_name} status=ok latency={latency_ms}ms")
            from app.core.metrics import metrics

            metrics.increment(f"mcp.calls.{client}")
            return {
                "content": [{"type": "text", "text": result_text}],
                "latency_ms": latency_ms,
            }, 200
        except Exception as e:
            log.error(f"mcp.audit client={client} tool={tool_name} status=error: {e}")
            return {"error": {"code": -32000, "message": str(e)[:200]}}, 500

    if method == "initialize":
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "github-autopilot", "version": __version__},
        }, 200

    return {"error": {"code": -32601, "message": f"Unknown method: {method}"}}, 400
