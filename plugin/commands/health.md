---
description: Report a repository's health checks
argument-hint: <owner/repo>
---
Use the `get_repo_health` tool from the **github-autopilot** MCP server for
repository **$1**.

Present exactly what the tool returns: its checks (open issues, open PRs,
license, description) and its recommendations. It does not measure CI, test
coverage, security or dependencies — do not add a grade, a score or any
dimension it did not report. If `$1` is missing, ask the user which repository
(owner/repo) to check.
