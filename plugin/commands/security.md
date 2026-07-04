---
description: Scan code for exposed secrets and vulnerabilities
argument-hint: [file or paste code]
---
Security-review the code in **$ARGUMENTS** (a file path, or code the user pasted)
using the **github-autopilot** MCP server:

1. Call `scan_secrets` to detect exposed credentials (API keys, tokens, private
   keys) with entropy gating.
2. Call `security_review` for CVE and vulnerability analysis.

Report the risk level, each finding with severity, and the fix. If nothing was
provided in `$ARGUMENTS`, ask the user for a file path or code snippet to scan.
Never echo a detected secret in full — keep it redacted.
