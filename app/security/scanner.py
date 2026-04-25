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
        return {"critical": 4, "high": 3, "medium": 2, "low": 1}.get(
            self.severity.lower(), 0
        )


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

    def to_markdown(self, include_low: bool = False) -> str:
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

        lines.append(
            f"**{total} finding(s)** — {', '.join(sev_line) if sev_line else 'low/medium only'}\n"
        )
        lines.append("| Source | Critical | High | Medium | Low |")
        lines.append("|--------|----------|------|--------|-----|")

        for source_name, findings in [
            ("Dependabot", self.dependabot),
            ("CodeQL", self.codeql),
            ("Secret Scanning", self.secrets),
        ]:
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
                lines.append(
                    f"- {sev_emoji} **{f.severity.upper()}**{pkg_str}{cve_str}: {f.title}"
                )

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
        lines.append("*🤖 AI Repo Manager V4 — GitHub Security APIs*")
        return "\n".join(lines)


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
        report.codeql = [
            f for f in all_codeql if not f.file_path or f.file_path in changed_paths
        ]
    else:
        report.codeql = all_codeql

    report.dependabot = all_dep
    report.secrets = all_sec

    return report


def _scan_dependabot(repo: str, token: str, errors: list) -> list[SecurityFinding]:
    try:
        from app.github.client import gh_get

        alerts = gh_get(
            f"/repos/{repo}/dependabot/alerts?state=open&per_page=30", token
        )
        findings = []
        for alert in alerts:
            adv = alert.get("security_advisory", {})
            dep = alert.get("dependency", {})
            pkg = dep.get("package", {}).get("name", "")
            severity = adv.get("severity", "medium").lower()
            cve_ids = [
                i["value"] for i in adv.get("identifiers", []) if i["type"] == "CVE"
            ]
            ghsa_ids = [
                i["value"] for i in adv.get("identifiers", []) if i["type"] == "GHSA"
            ]
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
        err = str(e)
        if "403" in err or "404" in err:
            errors.append("Dependabot (not enabled or no permission)")
        else:
            log.warning(f"dependabot scan failed: {e}")
        return []


def _scan_codeql(repo: str, token: str, errors: list) -> list[SecurityFinding]:
    try:
        from app.github.client import gh_get

        alerts = gh_get(
            f"/repos/{repo}/code-scanning/alerts?state=open&per_page=30", token
        )
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
                    title=rule.get("description", rule.get("id", "CodeQL finding"))[
                        :100
                    ],
                    description=alert.get("message", {}).get("text", "")[:200],
                    file_path=location.get("path", ""),
                    line_number=location.get("start_line", 0),
                    url=alert.get("html_url", ""),
                )
            )
        return findings
    except Exception as e:
        err = str(e)
        if "403" in err or "404" in err:
            errors.append("CodeQL (not enabled or no permission)")
        else:
            log.warning(f"codeql scan failed: {e}")
        return []


def _scan_secrets(repo: str, token: str, errors: list) -> list[SecurityFinding]:
    try:
        from app.github.client import gh_get

        alerts = gh_get(
            f"/repos/{repo}/secret-scanning/alerts?state=open&per_page=30", token
        )
        findings = []
        for alert in alerts:
            secret_type = alert.get(
                "secret_type_display_name", alert.get("secret_type", "Secret")
            )
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
        err = str(e)
        if "403" in err or "404" in err:
            errors.append("Secret Scanning (not enabled or no permission)")
        else:
            log.warning(f"secret scanning failed: {e}")
        return []
