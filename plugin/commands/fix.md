---
description: Get a root-cause analysis and production-ready fix for a GitHub issue
argument-hint: <owner/repo> <issue-number>
---
Use the `fix_issue` tool from the **github-autopilot** MCP server to analyze
issue **$2** in repository **$1**.

Present:

1. The root cause, in plain language.
2. The suggested fix (with a code snippet where useful).
3. A verification test that proves the fix works.

If the tool needs more context (a stack trace or the failing code), ask the user
for it and pass it along. If `$1` or `$2` is missing, ask for the repo and issue
number first.
