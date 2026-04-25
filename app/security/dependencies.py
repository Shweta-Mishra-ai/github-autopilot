"""
app/security/dependencies.py
V4 Sprint 2: Smarter dependency scanner.

FIXED: No more duplicate issues on every push.
NEW: Severity filter — only HIGH/CRITICAL create issues by default.
NEW: Known/accepted CVE suppression list.
NEW: Issue only when new HIGH finding appears (not same LOW every time).
"""

import re
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# ── Known vulnerability database (subset — most common packages) ─────────────
# Format: (package, version_pattern, severity, cve_id, description)
KNOWN_VULNS: list[tuple] = [
    # Flask
    ("flask", r"3\.", "LOW", "GHSA-68rp-wp8r-4726", "Missing Vary:Cookie header"),
    # Requests
    (
        "requests",
        r"2\.3",
        "MODERATE",
        "GHSA-gc5v-m9x4-r6x2",
        "Insecure Temp File Reuse",
    ),
    ("requests", r"2\.3", "MODERATE", "GHSA-9wx4-h78v-vm56", "Credential leak via URL"),
    # Cryptography 42.x
    (
        "cryptography",
        r"42\.",
        "LOW",
        "GHSA-79v4-65xg-pq4g",
        "Vulnerable OpenSSL wheels",
    ),
    (
        "cryptography",
        r"42\.",
        "LOW",
        "GHSA-m959-cc7f-wv43",
        "Incomplete DNS constraint",
    ),
    # Cryptography 43.x (WORSE — HIGH severity added)
    (
        "cryptography",
        r"43\.",
        "LOW",
        "GHSA-79v4-65xg-pq4g",
        "Vulnerable OpenSSL wheels",
    ),
    (
        "cryptography",
        r"43\.",
        "LOW",
        "GHSA-m959-cc7f-wv43",
        "Incomplete DNS constraint",
    ),
    (
        "cryptography",
        r"43\.",
        "HIGH",
        "GHSA-r6ph-v2qm-q3c2",
        "Subgroup Attack SECT Curves",
    ),
    # Cryptography 46.x (Render build fails — needs Rust)
    (
        "cryptography",
        r"46\.",
        "LOW",
        "GHSA-m959-cc7f-wv43",
        "Incomplete DNS constraint",
    ),
    ("cryptography", r"46\.", "BUILD", "RENDER-001", "Needs Rust — fails on free tier"),
]

# Accepted/suppressed CVEs — LOW severity we've acknowledged and accepted
ACCEPTED_CVES: set[str] = {
    "GHSA-68rp-wp8r-4726",  # Flask Vary:Cookie — no patch, LOW risk
    "GHSA-gc5v-m9x4-r6x2",  # Requests temp file — LOW risk for our use
    "GHSA-79v4-65xg-pq4g",  # Cryptography OpenSSL wheels — LOW
}

# Only create GitHub issues for these severities
ISSUE_SEVERITIES: set[str] = {"HIGH", "CRITICAL"}


@dataclass
class DepFinding:
    package: str
    version: str
    severity: str
    cve_id: str
    description: str

    @property
    def is_actionable(self) -> bool:
        """True if this finding should trigger a GitHub issue."""
        return self.severity in ISSUE_SEVERITIES and self.cve_id not in ACCEPTED_CVES


def scan_requirements_txt(content: str) -> list[DepFinding]:
    """
    Scan requirements.txt content for known vulnerabilities.
    Returns ALL findings (caller decides what to act on).
    """
    findings = []
    lines = content.strip().splitlines()

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Parse package==version
        match = re.match(r"^([a-zA-Z0-9_\-\[\]]+)==([^\s#]+)", line)
        if not match:
            continue

        pkg = match.group(1).lower().split("[")[0]  # strip extras like [redis]
        version = match.group(2)

        for vuln_pkg, ver_pattern, severity, cve_id, desc in KNOWN_VULNS:
            if pkg == vuln_pkg and re.match(ver_pattern, version):
                findings.append(
                    DepFinding(
                        package=pkg,
                        version=version,
                        severity=severity,
                        cve_id=cve_id,
                        description=desc,
                    )
                )

    return findings


def get_actionable_findings(findings: list[DepFinding]) -> list[DepFinding]:
    """Filter to only HIGH/CRITICAL unaccepted findings."""
    return [f for f in findings if f.is_actionable]


def format_dep_findings(findings: list[DepFinding]) -> str:
    """
    Format findings as GitHub issue body.
    Groups by severity — HIGH first.
    """
    if not findings:
        return ""

    high = [f for f in findings if f.severity in ("HIGH", "CRITICAL")]
    moderate = [f for f in findings if f.severity == "MODERATE"]
    low = [f for f in findings if f.severity == "LOW"]

    lines = ["## ⚠️ Dependency Vulnerabilities Found\n"]
    lines.append(f"{len(findings)} package(s) have known vulnerabilities.\n")

    def _render(group: list[DepFinding]):
        for f in group:
            sev_emoji = {
                "HIGH": "🔴",
                "CRITICAL": "🚨",
                "MODERATE": "🟡",
                "LOW": "🟢",
            }.get(f.severity, "⚠️")
            lines.append(f"\n`{f.package}=={f.version}`")
            lines.append(
                f"- {sev_emoji} [{f.cve_id}](https://github.com/advisories/{f.cve_id}) "
                f"({f.severity}): {f.description}"
            )

    if high:
        lines.append("\n### 🔴 High Severity")
        _render(high)
    if moderate:
        lines.append("\n### 🟡 Moderate Severity")
        _render(moderate)
    if low:
        lines.append("\n### 🟢 Low Severity (informational)")
        _render(low)

    lines.append("\n---")
    lines.append("Run `pip install --upgrade <package>` to update affected packages.")

    return "\n".join(lines)
