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
from ._client import gh_get  # noqa: F401  (re-exported: tests patch these names)


log = logging.getLogger(__name__)


def _pr_head_sha(repo: str, pr_number: int, token: str) -> str:
    """
    Head commit SHA of a PR, or "" if it cannot be resolved.

    Returning "" degrades to a default-branch read rather than failing the
    whole scan — the secrets half of the report is still worth posting.
    """
    try:
        return (gh_get(f"/repos/{repo}/pulls/{pr_number}", token).get("head") or {}).get("sha", "")
    except Exception as exc:
        log.warning(f"security.head_sha_failed repo={repo} pr={pr_number}: {exc}")
        return ""


def cmd_security(repo: str, issue_number: int, issue: dict, token: str) -> str:
    """Scan PR files for secrets and vulnerable dependencies."""
    if "pull_request" not in issue:
        return "## ℹ️ `/security` works best on Pull Requests."

    try:
        from app.security.enhanced_secrets import format_findings as fmt_secrets, scan_diff
        from app.security.dependencies import scan_requirements_txt, format_dep_findings

        from app.github.helpers import pr_files as fetch_pr_files

        pr_files = fetch_pr_files(repo, issue_number, token, get=gh_get)
        all_findings = []
        scanned = 0
        unscannable: list[str] = []
        # Every file. This scanned `pr_files[:10]` and then reported "No
        # secrets detected in changed files" — a claim about every file,
        # backed by at most ten of them. The scan is local pattern matching,
        # so there is no cost reason to stop early.
        for f in pr_files:
            # `or ""` — GitHub sends an explicit null patch for binary files
            # and oversized diffs, which is not the same as an absent key.
            patch = f.get("patch") or ""
            if patch:
                scanned += 1
                all_findings.extend(scan_diff(patch, file_path=f.get("filename", "")))
            elif f.get("status") != "removed":
                # Not scanned, and the report must say so rather than let the
                # file stand inside a "no secrets detected" claim.
                unscannable.append(f.get("filename", "?"))

        dep_findings = []
        dep_scanned = False
        dep_error = ""
        # The contents API defaults to the repository's default branch. Reading
        # requirements.txt without a ref therefore scanned the *base* file and
        # reported "no vulnerable dependencies" for a PR whose whole change was
        # adding one. Pin the read to the PR head instead.
        head_sha = _pr_head_sha(repo, issue_number, token)
        for f in pr_files:
            # A REMOVED requirements.txt has nothing at the head to read: the
            # 404 used to abort the whole scan, discarding the secret findings
            # already computed above.
            if f.get("filename") != "requirements.txt" or f.get("status") == "removed":
                continue
            import base64

            path = "/repos/{}/contents/requirements.txt".format(repo)
            if head_sha:
                path += f"?ref={head_sha}"
            try:
                raw = gh_get(path, token)
                content = base64.b64decode(raw["content"]).decode()
                dep_findings.extend(scan_requirements_txt(content))
                dep_scanned = True
            except Exception as e:
                dep_error = f"{type(e).__name__}"

        lines = ["## 🔒 Security Scan Results\n"]
        if all_findings:
            lines.append(fmt_secrets(all_findings, repo))
        elif scanned:
            lines.append(f"✅ **No secrets detected** in the {scanned} changed file(s) scanned.\n")
        else:
            lines.append("ℹ️ **No file diffs could be scanned for secrets.**\n")
        if unscannable:
            shown = ", ".join(f"`{n}`" for n in unscannable[:10])
            more = f" and {len(unscannable) - 10} more" if len(unscannable) > 10 else ""
            lines.append(
                f"⚠️ **{len(unscannable)} file(s) not scanned** — GitHub sends no diff for "
                f"binary files or very large changes: {shown}{more}. Check these by hand.\n"
            )
        # Only what was measured. "No vulnerable dependencies found" was printed
        # whether or not anything was scanned — for a PR touching only
        # package.json, or none of its dependencies at all.
        if dep_findings:
            lines.append(format_dep_findings(dep_findings))
        elif dep_scanned:
            lines.append("✅ **No vulnerable dependencies** found in `requirements.txt`.\n")
        elif dep_error:
            lines.append(
                f"⚠️ **Dependencies not checked** — `requirements.txt` could not be read ({dep_error}).\n"
            )
        else:
            lines.append(
                "ℹ️ **Dependencies not checked** — this PR does not change a root "
                "`requirements.txt`, the only manifest this command checks.\n"
            )
        return "\n\n".join(lines)

    except Exception as exc:
        return fmt_error("Security scan failed", exc)


def _license_section(repo: str, token: str) -> str:
    """
    License-compliance section of the full scan, or "" if it cannot be run.

    app/security/licenses.py was written, tested and then never imported, so
    the bot has never once reported a copyleft dependency. `/secfull` is the
    natural home: it is the only command that scans the whole repository
    rather than a diff.

    Returns "" — not an error block — when there is no requirements.txt or the
    check fails. A licence report is advisory; it must never be the reason a
    security scan comes back empty.
    """
    try:
        import base64

        from app.security.licenses import format_findings, scan_requirements_report

        raw = gh_get(f"/repos/{repo}/contents/requirements.txt", token)
        if not isinstance(raw, dict) or not raw.get("content"):
            return ""
        content = base64.b64decode(raw["content"]).decode("utf-8", errors="replace")
        report = scan_requirements_report(content)
        if not report["total"]:
            return ""
        return format_findings(report["findings"], report["total"], report["checked"])
    except Exception as exc:
        log.info(f"security.license_scan_skipped repo={repo}: {exc}")
        return ""


def cmd_secfull(repo: str, token: str) -> str:
    """Full repository security scan."""
    try:
        from app.security.scanner import run_security_scan

        report = run_security_scan(repo, token)
        sections = [report.to_markdown(include_low=True)]

        licenses = _license_section(repo, token)
        if licenses:
            sections.append(licenses)

        return "\n\n---\n\n".join(sections)
    except Exception as exc:
        return fmt_error("Security scan failed", exc)
