---
description: Grade a repository's overall health
argument-hint: <owner/repo>
---
Use the `get_repo_health` tool from the **github-autopilot** MCP server to grade
repository **$1**.

Present the grade and score, the dimension breakdown (CI/CD, test coverage,
security, docs, dependencies), the top issues, and the quick wins. If `$1` is
missing, ask the user which repository (owner/repo) to grade.
