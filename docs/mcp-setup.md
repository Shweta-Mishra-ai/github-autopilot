# MCP Setup — use GitHub Autopilot from your IDE

GitHub Autopilot exposes an [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server at
`POST /mcp` (JSON-RPC 2.0 over HTTP). Register it once and your AI IDE can call
Autopilot tools — analyze PRs, scan secrets, generate tests — directly from chat.

## Auth (required)

The endpoint **fails closed**: if `MCP_API_KEY` is unset on the server, every
request is rejected with `503`. Generate a key and set it in your Render env:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Every client request must send `Authorization: Bearer <MCP_API_KEY>`.

Optional hardening: set `MCP_ALLOWED_INSTALLATIONS` (comma-separated GitHub App
installation IDs) so a leaked key cannot act on other installations.

## Register in your client

### Claude Code (CLI)

```bash
claude mcp add --transport http github-autopilot \
  https://your-app.onrender.com/mcp \
  --header "Authorization: Bearer YOUR_MCP_API_KEY"
```

### Claude Desktop / claude.ai — `mcp.json`

```json
{
  "mcpServers": {
    "github-autopilot": {
      "type": "http",
      "url": "https://your-app.onrender.com/mcp",
      "headers": { "Authorization": "Bearer YOUR_MCP_API_KEY" }
    }
  }
}
```

### Cursor — `.cursor/mcp.json`

```json
{
  "mcpServers": {
    "github-autopilot": {
      "url": "https://your-app.onrender.com/mcp",
      "headers": { "Authorization": "Bearer YOUR_MCP_API_KEY" }
    }
  }
}
```

### Codex CLI — `~/.codex/config.toml`

```toml
[mcp_servers.github-autopilot]
url = "https://your-app.onrender.com/mcp"
http_headers = { "Authorization" = "Bearer YOUR_MCP_API_KEY" }
```

## Available tools

| Tool | What it does |
|------|--------------|
| `analyze_pr` | Grade a PR (A–F): quality, security, test gaps, blast radius |
| `fix_issue` | Root cause + production-ready fix + verification test for an issue |
| `scan_secrets` | Scan a code snippet for exposed credentials (regex + entropy) |
| `explain_code` | Plain-English explanation at brief/standard/deep depth |
| `generate_tests` | pytest/unittest suite for a function or class |
| `security_review` | CVE + vulnerability review of code or requirements.txt |
| `get_repo_health` | Repo health grade with dimensions and quick wins |
| `run_command` | Run read-only slash commands (`/fix /explain /security …`) remotely |

Destructive commands (`/merge`, `/autofix`, `/apply`, `/rollback`, `/release`)
are **not** available via MCP by design — they require a GitHub comment so
there is a visible audit trail.

## Verify it works

```bash
# Discovery (no auth)
curl https://your-app.onrender.com/mcp

# List tools (auth required)
curl -X POST https://your-app.onrender.com/mcp \
  -H "Authorization: Bearer YOUR_MCP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"method": "tools/list", "params": {}}'

# Call a tool
curl -X POST https://your-app.onrender.com/mcp \
  -H "Authorization: Bearer YOUR_MCP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"method": "tools/call", "params": {"name": "scan_secrets", "arguments": {"content": "api_key = \"AKIAIOSFODNN7EXAMPLE\""}}}'
```

Expected discovery response: server name, protocol `mcp/2024-11-05`, tool count.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `503 Server auth not configured` | `MCP_API_KEY` unset on server | Set it in Render env vars and redeploy |
| `401 Unauthorized` | Wrong/missing bearer token | Match client header to server `MCP_API_KEY` |
| `403 installation not allowed` | `MCP_ALLOWED_INSTALLATIONS` excludes your install id | Add the id or unset the allowlist |
| Timeouts | Render free tier cold start (~30s) | Retry once; keep-alive ping helps |
