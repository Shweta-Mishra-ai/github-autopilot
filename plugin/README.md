# GitHub Autopilot — Claude Code plugin

Call GitHub Autopilot's AI tools straight from Claude Code, Cursor, or any
MCP-capable IDE. Backed by your deployed Autopilot server over MCP.

## Install

```
/plugin marketplace add Shweta-Mishra-ai/github-autopilot
/plugin install github-autopilot
```

## Configure

The plugin talks to *your* deployed Autopilot server. Set two environment
variables before launching your IDE:

```bash
export GITHUB_AUTOPILOT_URL="https://github-autopilot-1.onrender.com/mcp"
export MCP_API_KEY="<the MCP_API_KEY you set on the server>"
```

Verify the server is reachable:

```bash
curl -s "$GITHUB_AUTOPILOT_URL" | jq .name   # → "github-autopilot"
```

## Commands

| Command | Does |
|---------|------|
| `/github-autopilot:review <owner/repo> <pr#>` | Grade a PR — quality, security, test gaps, blast radius |
| `/github-autopilot:fix <owner/repo> <issue#>` | Root cause + fix + verification test |
| `/github-autopilot:security [file]` | Scan for exposed secrets and CVEs |
| `/github-autopilot:health <owner/repo>` | Repository health grade with quick wins |

All commands call the `github-autopilot` MCP server defined in
[`.mcp.json`](.mcp.json). The server is **fail-closed**: if `MCP_API_KEY` is unset
on the server side it rejects every request, so the endpoint is never open.

## Requirements

- A deployed GitHub Autopilot instance ([1-click deploy](https://github.com/Shweta-Mishra-ai/github-autopilot#deploy-in-10-minutes)).
- `MCP_API_KEY` configured on that instance.
