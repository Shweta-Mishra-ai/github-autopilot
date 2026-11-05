---
description: Analyze a GitHub PR for quality, security, and test gaps
argument-hint: <owner/repo> <pr-number>
---
Use the `analyze_pr` tool from the **github-autopilot** MCP server to analyze
pull request **$2** in repository **$1**.

Ask it to focus on code quality, security risks, test-coverage gaps, and blast
radius. Then present, concisely and only from what the tool returned:

1. Its recommendation (approve / request changes) and summary.
2. Its findings, most severe first.
3. Its improvement suggestions.

The tool gives no grade or score — do not add one, or any finding it did not
report. If it returns an error, say so.

If `$1` or `$2` is missing, ask the user for the repo (owner/repo) and PR number.
