"""app/mcp — Model Context Protocol server for GitHub Autopilot."""

# Support both filename variants (server.py and mcp_server.py)
try:
    from app.mcp.server import handle_mcp_request, MCP_TOOLS, TOOL_HANDLERS
except ImportError:
    from app.mcp.mcp_server import handle_mcp_request, MCP_TOOLS, TOOL_HANDLERS

__all__ = ["handle_mcp_request", "MCP_TOOLS", "TOOL_HANDLERS"]
