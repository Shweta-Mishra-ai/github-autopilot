"""
app/handlers/comments/security.py
Security scan commands: /security, /secfull.

Split out of publisher.py, which had grown past the package's 600-line ceiling.
These two are read-only scans with no GitHub writes, so they never belonged
with the publishing commands anyway.
"""

from __future__ import annotations

import logging

from app.github.helpers import fmt_error
import app.handlers.comments as hc

log = logging.getLogger(__name__)


def gh_get(*a, **kw):
    return hc.gh_get(*a, **kw)


def cmd_security(repo: str, issue_number: int, issue: dict, token: str) -> str:
    """Scan PR files for secrets and vulnerable dependencies."""
    if "pull_request" not in issue:
        return "## ℹ️ `/security` works best on Pull Requests."

    try:
        from app.security.enhanced_secrets import format_findings as fmt_secrets, scan_diff
        from app.security.dependencies import scan_requirements_txt, format_dep_findings

        pr_files = gh_get(f"/repos/{repo}/pulls/{issue_number}/files", token)
        all_findings = []
        for f in pr_files[:10]:
            patch = f.get("patch", "")
            if patch:
                all_findings.extend(scan_diff(patch, file_path=f.get("filename", "")))

        dep_findings = []
        for f in pr_files:
            if f["filename"] == "requirements.txt":
                import base64

                raw = gh_get(f"/repos/{repo}/contents/{f['filename']}", token)
                content = base64.b64decode(raw["content"]).decode()
                dep_findings.extend(scan_requirements_txt(content))

        lines = ["## 🔒 Security Scan Results\n"]
        lines.append(
            fmt_secrets(all_findings, repo)
            if all_findings
            else "✅ **No secrets detected** in changed files.\n"
        )
        lines.append(
            format_dep_findings(dep_findings)
            if dep_findings
            else "✅ **No vulnerable dependencies** found.\n"
        )
        return "\n\n".join(lines)

    except Exception as exc:
        return fmt_error("Security scan failed", exc)


def cmd_secfull(repo: str, token: str) -> str:
    """Full repository security scan."""
    try:
        from app.security.scanner import run_security_scan

        report = run_security_scan(repo, token)
        return report.to_markdown(include_low=True)
    except Exception as exc:
        return fmt_error("Security scan failed", exc)
