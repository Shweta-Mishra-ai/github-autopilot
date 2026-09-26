"""
app/security/scanner.py
V4 Sprint 4: GitHub Security APIs scanner.

Reads from 3 free GitHub Security APIs:
  1. Dependabot Alerts     — vulnerable dependencies
  2. Code Scanning (CodeQL) — code vulnerabilities
  3. Secret Scanning       — exposed secrets

No custom scanning needed — GitHub already runs these.
We just read and format the results.

Usage:
    from app.security.scanner import SecurityReport, run_security_scan
    report = run_security_scan(repo, token)
    markdown = report.to_markdown()
"""

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class SecurityFinding:
    source: str
    severity: str
    title: str
    description: str
    package: str = ""
    cve_id: str = ""
    file_path: str = ""
    line_number: int = 0
    url: str = ""

    @property
    def severity_rank(self) -> int:
        return {"critical": 4, "high": 3, "medium": 2, "low": 1}.get(self.severity.lower(), 0)


@dataclass
class SecurityReport:
    repo: str
    dependabot: list[SecurityFinding] = field(default_factory=list)
    codeql: list[SecurityFinding] = field(default_factory=list)
    secrets: list[SecurityFinding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def all_findings(self) -> list[SecurityFinding]:
        combined = self.dependabot + self.codeql + self.secrets
        return sorted(combined, key=lambda f: f.severity_rank, reverse=True)

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.all_findings if f.severity == "critical")

    @property
    def high_count(self) -> int:
        return sum(1 for f in self.all_findings if f.severity == "high")

    @property
    def total_count(self) -> int:
        return len(self.all_findings)

    @property
    def unavailable(self) -> list[str]:
        """Sources that could not be read, in SOURCES order."""
        return [src for src in SOURCES if any(e.startswith(f"{src} (") for e in self.errors)]

    @property
    def scanned_nothing(self) -> bool:
        return len(self.unavailable) == len(SOURCES)

    def to_markdown(self, include_low: bool = False) -> str:
        if self.scanned_nothing:
            # Not "All Clear", and not a table of zeros either: a row of zeros
            # for a source that was never read is the same false claim in a
            # different shape.
            reasons = "\n".join(f"- {e}" for e in self.errors)
            return (
                "## 🔒 Security Report — Not Scanned\n\n"
                "⚠️ None of the three sources could be read, so this is **not** "
                "a clean result:\n\n"
                f"{reasons}\n\n"
                "Grant the GitHub App read access to *Dependabot alerts*, *Code "
                "scanning alerts* and *Secret scanning alerts*, and enable those "
                "features on the repository.\n\n"
                f"*Repository: `{self.repo}`*"
            )

        if self.total_count == 0 and not self.errors:
            return (
                "## 🔒 Security Report — All Clear\n\n"
                "✅ No security findings from Dependabot, CodeQL, or Secret Scanning.\n\n"
                f"*Scanned: Dependabot, CodeQL, Secret Scanning*\n"
                f"*Repository: `{self.repo}`*"
            )

        lines = [f"## 🔒 Security Report — `{self.repo}`\n"]

        total = self.total_count
        crit = self.critical_count
        high = self.high_count
        sev_line = []
        if crit:
            sev_line.append(f"🚨 {crit} critical")
        if high:
            sev_line.append(f"🔴 {high} high")

        if total:
            summary = ", ".join(sev_line) if sev_line else "low/medium only"
        else:
            summary = "none in the sources that could be read"
        lines.append(f"**{total} finding(s)** — {summary}\n")
        lines.append("| Source | Critical | High | Medium | Low |")
        lines.append("|--------|----------|------|--------|-----|")

        for source_name, findings in [
            ("Dependabot", self.dependabot),
            ("CodeQL", self.codeql),
            ("Secret Scanning", self.secrets),
        ]:
            if source_name in self.unavailable:
                lines.append(f"| {source_name} | — | — | — | — |")
                continue
            c = sum(1 for f in findings if f.severity == "critical")
            h = sum(1 for f in findings if f.severity == "high")
            m = sum(1 for f in findings if f.severity == "medium")
            low = sum(1 for f in findings if f.severity == "low")
            lines.append(f"| {source_name} | {c} | {h} | {m} | {low} |")

        if self.dependabot:
            lines.append("\n### 📦 Dependabot Alerts")
            for f in self.dependabot:
                if not include_low and f.severity == "low":
                    continue
                sev_emoji = {
                    "critical": "🚨",
                    "high": "🔴",
                    "medium": "🟡",
                    "low": "🟢",
                }.get(f.severity, "⚠️")
                pkg_str = f" — `{f.package}`" if f.package else ""
                cve_str = f" ([{f.cve_id}]({f.url}))" if f.cve_id else ""
                lines.append(f"- {sev_emoji} **{f.severity.upper()}**{pkg_str}{cve_str}: {f.title}")

        if self.codeql:
            lines.append("\n### 🔍 CodeQL Findings")
            for f in self.codeql:
                if not include_low and f.severity == "low":
                    continue
                sev_emoji = {
                    "critical": "🚨",
                    "high": "🔴",
                    "medium": "🟡",
                    "low": "🟢",
                }.get(f.severity, "⚠️")
                loc = f" `{f.file_path}:{f.line_number}`" if f.file_path else ""
                lines.append(f"- {sev_emoji} **{f.severity.upper()}**{loc}: {f.title}")

        if self.secrets:
            lines.append("\n### 🗝️ Secret Scanning")
            for f in self.secrets:
                lines.append(f"- 🚨 **{f.title}**: {f.description[:100]}")

        if self.errors:
            lines.append(f"\n> ⚠️ Some APIs unavailable: {', '.join(self.errors)}")

        lines.append("\n---")
        lines.append("*🤖 GitHub Autopilot — GitHub Security APIs*")
        return "\n".join(lines)


SOURCES = ("Dependabot", "CodeQL", "Secret Scanning")


def _record_failure(source: str, e: Exception, errors: list) -> None:
    """
    Record that `source` could not be read. Every failure is recorded.

    This matched `"403" in str(e) or "404" in str(e)`, and the GitHub client's
    messages are "Forbidden: ..." and "Not found: ..." — the status lives in
    `e.status_code`, not in the text. So a 403 (the App was never granted the
    security permission — the commonest real case), a 404 (the feature is off),
    a 5xx and a timeout ALL fell through to a log line, left `errors` empty, and
    the report said "All Clear" for a repository it had not read at all. The
    unit tests raised Exception("403 Forbidden"), a message the real client
    never produces, which is why they passed.
    """
    status = getattr(e, "status_code", None)
    if status in (403, 404) or (status is None and re.search(r"\b40[34]\b", str(e))):
        errors.append(f"{source} (not enabled or no permission)")
    else:
        errors.append(f"{source} (unavailable: {str(e)[:80]})")
    log.warning(f"security.source_unavailable source={source}: {e}")


def _alerts(path: str, token: str) -> list:
    """GET a list of alerts, refusing anything that is not a list."""
    from app.github.client import gh_get

    alerts = gh_get(path, token)
    if not isinstance(alerts, list):
        # Iterating a dict walks its keys, and `"key".get(...)` raised
        # AttributeError into the same silent branch as everything else.
        raise ValueError(f"expected a list of alerts, got {type(alerts).__name__}")
    return alerts


def run_security_scan(repo: str, token: str) -> SecurityReport:
    report = SecurityReport(repo=repo)
    report.dependabot = _scan_dependabot(repo, token, report.errors)
    report.codeql = _scan_codeql(repo, token, report.errors)
    report.secrets = _scan_secrets(repo, token, report.errors)

    log.info(
        f"security.scan_complete repo={repo} total={report.total_count} critical={report.critical_count}"
    )
    return report


def run_pr_security_scan(repo: str, pr_number: int, token: str) -> SecurityReport:
    from app.github.client import gh_get

    report = SecurityReport(repo=repo)

    try:
        pr_files = gh_get(f"/repos/{repo}/pulls/{pr_number}/files", token)
        changed_paths = {f["filename"] for f in pr_files}
    except Exception:
        changed_paths = set()

    all_dep = _scan_dependabot(repo, token, report.errors)
    all_codeql = _scan_codeql(repo, token, report.errors)
    all_sec = _scan_secrets(repo, token, report.errors)

    if changed_paths:
        report.codeql = [f for f in all_codeql if not f.file_path or f.file_path in changed_paths]
    else:
        report.codeql = all_codeql

    report.dependabot = all_dep
    report.secrets = all_sec

    return report


def _scan_dependabot(repo: str, token: str, errors: list) -> list[SecurityFinding]:
    try:
        alerts = _alerts(f"/repos/{repo}/dependabot/alerts?state=open&per_page=30", token)
        findings = []
        for alert in alerts:
            adv = alert.get("security_advisory", {})
            dep = alert.get("dependency", {})
            pkg = dep.get("package", {}).get("name", "")
            severity = adv.get("severity", "medium").lower()
            cve_ids = [i["value"] for i in adv.get("identifiers", []) if i["type"] == "CVE"]
            ghsa_ids = [i["value"] for i in adv.get("identifiers", []) if i["type"] == "GHSA"]
            cve_id = cve_ids[0] if cve_ids else (ghsa_ids[0] if ghsa_ids else "")
            url = alert.get("html_url", "")

            findings.append(
                SecurityFinding(
                    source="dependabot",
                    severity=severity,
                    title=adv.get("summary", f"Vulnerability in {pkg}")[:100],
                    description=adv.get("description", "")[:200],
                    package=pkg,
                    cve_id=cve_id,
                    url=url,
                )
            )
        return findings
    except Exception as e:
        _record_failure("Dependabot", e, errors)
        return []


def _scan_codeql(repo: str, token: str, errors: list) -> list[SecurityFinding]:
    try:
        alerts = _alerts(f"/repos/{repo}/code-scanning/alerts?state=open&per_page=30", token)
        findings = []
        for alert in alerts:
            rule = alert.get("rule", {})
            location = alert.get("most_recent_instance", {}).get("location", {})
            severity = rule.get("severity", "medium").lower()
            if severity == "error":
                severity = "high"
            elif severity == "warning":
                severity = "medium"
            elif severity == "note":
                severity = "low"

            findings.append(
                SecurityFinding(
                    source="codeql",
                    severity=severity,
                    title=rule.get("description", rule.get("id", "CodeQL finding"))[:100],
                    description=alert.get("message", {}).get("text", "")[:200],
                    file_path=location.get("path", ""),
                    line_number=location.get("start_line", 0),
                    url=alert.get("html_url", ""),
                )
            )
        return findings
    except Exception as e:
        _record_failure("CodeQL", e, errors)
        return []


def _scan_secrets(repo: str, token: str, errors: list) -> list[SecurityFinding]:
    try:
        alerts = _alerts(f"/repos/{repo}/secret-scanning/alerts?state=open&per_page=30", token)
        findings = []
        for alert in alerts:
            secret_type = alert.get("secret_type_display_name", alert.get("secret_type", "Secret"))
            findings.append(
                SecurityFinding(
                    source="secret_scanning",
                    severity="critical",
                    title=f"Exposed {secret_type}",
                    description=f"Found in: {alert.get('html_url', '')}",
                    url=alert.get("html_url", ""),
                )
            )
        return findings
    except Exception as e:
        _record_failure("Secret Scanning", e, errors)
        return []
