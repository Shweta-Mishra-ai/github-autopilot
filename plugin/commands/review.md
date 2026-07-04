---
description: Analyze a GitHub PR for quality, security, and test gaps
argument-hint: <owner/repo> <pr-number>
---
Use the `analyze_pr` tool from the **github-autopilot** MCP server to analyze
pull request **$2** in repository **$1**.

Ask it to focus on code quality, security risks, test-coverage gaps, and blast
radius. Then present, concisely:

1. The overall grade (A–F) and a one-line rationale.
2. The top findings, most severe first.
3. Concrete improvement suggestions the author can act on before merging.

If `$1` or `$2` is missing, ask the user for the repo (owner/repo) and PR number.
