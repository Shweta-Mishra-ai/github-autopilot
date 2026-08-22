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
#
# Entries are half-open version RANGES, not regex prefixes:
#
#     (package, introduced_in, fixed_in, severity, cve_id, description)
#
# A version is vulnerable when  introduced_in <= version < fixed_in.
# `introduced_in=None` means "every version up to fixed_in".
# `fixed_in=None`      means "no fixed release exists yet".
#
# This replaces regex prefix matching, which produced false positives that a
# reader had no way to distinguish from real ones. `re.match(r"2\.3", v)` is a
# PREFIX test, so it matched requests 2.30 through 2.39 — including 2.33.0,
# the patched version this project pins — for a CVE that only affects 2.3.x.
# `r"3\."` for flask matched every 3.x release ever, forever, including
# releases published after the advisory was fixed.
#
# A CVE describes a range. Anything that is not a range comparison will
# eventually claim a patched release is vulnerable, and a scanner that cries
# wolf is one whose real findings get ignored.
#
# Every range below is an exact translation of the release series its regex was
# written to describe — `r"42\."` becomes [42.0.0, 43.0.0). Deliberately no new
# claims: widening one to "and everything after" is how a scanner starts
# reporting patched releases as broken, which is the bug being removed here.
#
# NOTE: this table is a small hand-maintained fallback, not a vulnerability
# database. GitHub's Dependabot alerts — already read by app/security/scanner.py
# — are authoritative, always current, and cover every ecosystem. Prefer
# `/secfull` over this. Anything added here should carry a range you can point
# at in the advisory, not a guess.
KNOWN_VULNS: list[tuple] = [
    # Flask 3.x series.
    ("flask", "3.0.0", "4.0.0", "LOW", "GHSA-68rp-wp8r-4726", "Missing Vary:Cookie header"),
    # Requests 2.3.x series. The old `r"2\.3"` prefix also matched 2.30–2.39,
    # so a pinned 2.33.0 was reported vulnerable for a 2.3.x advisory.
    (
        "requests",
        "2.3.0",
        "2.4.0",
        "MODERATE",
        "GHSA-gc5v-m9x4-r6x2",
        "Insecure Temp File Reuse",
    ),
    (
        "requests",
        "2.3.0",
        "2.4.0",
        "MODERATE",
        "GHSA-9wx4-h78v-vm56",
        "Credential leak via URL",
    ),
    # Cryptography 42.x series.
    (
        "cryptography",
        "42.0.0",
        "43.0.0",
        "LOW",
        "GHSA-79v4-65xg-pq4g",
        "Vulnerable OpenSSL wheels",
    ),
    (
        "cryptography",
        "42.0.0",
        "43.0.0",
        "LOW",
        "GHSA-m959-cc7f-wv43",
        "Incomplete DNS constraint",
    ),
    # Cryptography 43.x series.
    (
        "cryptography",
        "43.0.0",
        "44.0.0",
        "LOW",
        "GHSA-79v4-65xg-pq4g",
        "Vulnerable OpenSSL wheels",
    ),
    (
        "cryptography",
        "43.0.0",
        "44.0.0",
        "LOW",
        "GHSA-m959-cc7f-wv43",
        "Incomplete DNS constraint",
    ),
    (
        "cryptography",
        "43.0.0",
        "44.0.0",
        "HIGH",
        "GHSA-r6ph-v2qm-q3c2",
        "Subgroup Attack SECT Curves",
    ),
    # Cryptography 46.x series. Not a CVE — a deployment constraint: 46.x needs
    # a Rust toolchain the Render free tier lacks, so the build fails rather
    # than the app being vulnerable. Severity BUILD keeps it out of the
    # security-issue path. Scoped to 46.x: as "46 and above" it reported the
    # pinned 50.0.0, which builds fine, as broken.
    (
        "cryptography",
        "46.0.0",
        "47.0.0",
        "LOW",
        "GHSA-m959-cc7f-wv43",
        "Incomplete DNS constraint",
    ),
    (
        "cryptography",
        "46.0.0",
        "47.0.0",
        "BUILD",
        "RENDER-001",
        "Needs Rust — fails on free tier",
    ),
]

# Trailing non-numeric release segments (rc1, b2, .post1, +local) are stripped
# before comparison: 2.32.0rc1 orders below 2.32.0, which is what a range check
# needs, and a pre-release of the fixed version is not itself the fix.
_VERSION_PART_RE = re.compile(r"^(\d+)")


def parse_version(version: str) -> tuple[int, ...] | None:
    """
    Parse a version string into a comparable tuple of ints.

    Returns None for anything unparseable — the caller then declines to make a
    claim rather than guessing. Reporting nothing about a version we cannot
    read is correct; reporting a vulnerability we cannot substantiate is not.

        "2.33.0"    -> (2, 33, 0)
        "2.32.0rc1" -> (2, 32, 0)
        "1.2"       -> (1, 2)
        "main"      -> None
    """
    if not version:
        return None
    # Drop local/build metadata before splitting.
    cleaned = str(version).strip().split("+", 1)[0]
    parts: list[int] = []
    for segment in cleaned.split("."):
        m = _VERSION_PART_RE.match(segment)
        if not m:
            break
        parts.append(int(m.group(1)))
    return tuple(parts) or None


def _pad(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[tuple, tuple]:
    """Zero-pad two version tuples to equal length so 2.32 == 2.32.0."""
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)), b + (0,) * (n - len(b))


def is_vulnerable(version: str, introduced_in: str | None, fixed_in: str | None) -> bool:
    """
    True when `version` falls in the half-open range [introduced_in, fixed_in).

    An unparseable version is never reported as vulnerable — see parse_version.
    """
    current = parse_version(version)
    if current is None:
        return False

    if introduced_in:
        low = parse_version(introduced_in)
        if low is not None:
            c, lo = _pad(current, low)
            if c < lo:
                return False

    if fixed_in:
        high = parse_version(fixed_in)
        if high is not None:
            c, hi = _pad(current, high)
            if c >= hi:
                return False

    return True


# Accepted/suppressed CVEs — LOW severity we've acknowledged and accepted
ACCEPTED_CVES: set[str] = {
    "GHSA-68rp-wp8r-4726",  # Flask Vary:Cookie — no patch, LOW risk
    "GHSA-gc5v-m9x4-r6x2",  # Requests temp file — LOW risk for our use
    "GHSA-79v4-65xg-pq4g",  # Cryptography OpenSSL wheels — LOW
}

# Only create GitHub issues for these severities
ISSUE_SEVERITIES: set[str] = {"HIGH", "CRITICAL"}


def format_range(introduced_in: str | None, fixed_in: str | None) -> str:
    """Render a half-open range the way a requirements specifier reads."""
    parts = []
    if introduced_in:
        parts.append(f">={introduced_in}")
    if fixed_in:
        parts.append(f"<{fixed_in}")
    return ",".join(parts)


@dataclass
class DepFinding:
    package: str
    version: str
    severity: str
    cve_id: str
    description: str
    # The version range this advisory was matched against, rendered for a
    # human: ">=3.0.0,<4.0.0". Shown instead of a synthesised "upgrade to X"
    # because the ranges in KNOWN_VULNS are release-series boundaries, not
    # fix releases — telling someone to install flask>=4.0.0, a version that
    # does not exist, is worse than telling them nothing. The range plus the
    # advisory link is information they can actually act on.
    affected_range: str = ""

    @property
    def is_actionable(self) -> bool:
        """True if this finding should trigger a GitHub issue."""
        return self.severity in ISSUE_SEVERITIES and self.cve_id not in ACCEPTED_CVES


# Matches `package==version`, tolerating extras (`celery[redis]==5.3.6`) and a
# trailing comment. Anything else — ranges, markers, -r includes, VCS URLs — is
# skipped, because a pin is the only form this scanner can match against a CVE.
_PIN_RE = re.compile(r"^([a-zA-Z0-9_\-\[\]]+)==([^\s#]+)")


def parse_requirements(content: str) -> list[dict]:
    """Parse requirements.txt content into a list of package dictionaries."""
    packages = []
    for line in (content or "").strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN_RE.match(line)
        if not match:
            continue
        packages.append(
            {
                "name": match.group(1).lower().split("[")[0],  # strip extras
                "version": match.group(2),
            }
        )
    return packages


def scan_requirements_txt(content: str) -> list[DepFinding]:
    """
    Scan requirements.txt content for known vulnerabilities.
    Returns ALL findings (caller decides what to act on).
    """
    # Parsing is delegated rather than repeated: this function used to carry a
    # byte-identical copy of the loop in parse_requirements(), so a fix to one
    # (e.g. tolerating a new pin syntax) silently missed the other.
    findings = []
    for pkg in parse_requirements(content):
        name, version = pkg["name"], pkg["version"]
        for vuln_pkg, introduced_in, fixed_in, severity, cve_id, desc in KNOWN_VULNS:
            if name != vuln_pkg:
                continue
            if not is_vulnerable(version, introduced_in, fixed_in):
                continue
            findings.append(
                DepFinding(
                    package=name,
                    version=version,
                    severity=severity,
                    cve_id=cve_id,
                    description=desc,
                    affected_range=format_range(introduced_in, fixed_in),
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
    # BUILD entries are deployment constraints, not vulnerabilities. They were
    # counted in the header and then rendered by no section at all, so the
    # reader saw "3 packages" above a list of two.
    build = [f for f in findings if f.severity == "BUILD"]
    # Catch-all. Every finding must appear somewhere: the buckets above are an
    # allow-list, so a severity nobody anticipated used to be counted and then
    # silently dropped. A security report that quietly omits a finding is worse
    # than one that renders it under an unfamiliar heading.
    known = {id(f) for f in high + moderate + low + build}
    other = [f for f in findings if id(f) not in known]
    vulns = high + moderate + low + other

    lines = ["## ⚠️ Dependency Findings\n"]
    if vulns:
        packages = len({f.package for f in vulns})
        lines.append(
            f"{len(vulns)} advisor{'y' if len(vulns) == 1 else 'ies'} "
            f"across {packages} package(s).\n"
        )

    def _render(group: list[DepFinding]):
        for f in group:
            sev_emoji = {
                "HIGH": "🔴",
                "CRITICAL": "🚨",
                "MODERATE": "🟡",
                "LOW": "🟢",
                "BUILD": "🔧",
            }.get(f.severity, "⚠️")
            lines.append(f"\n`{f.package}=={f.version}`")
            lines.append(
                f"- {sev_emoji} [{f.cve_id}](https://github.com/advisories/{f.cve_id}) "
                f"({f.severity}): {f.description}"
            )
            if f.affected_range:
                lines.append(
                    f"  - Affected: `{f.package}{f.affected_range}` — you have `{f.version}`"
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
    if other:
        lines.append("\n### ⚠️ Other")
        _render(other)
    if build:
        lines.append("\n### 🔧 Build Constraints")
        lines.append("\nNot vulnerabilities — these versions fail to build in this environment.")
        _render(build)

    lines.append("\n---")
    lines.append(
        "> Matched against version *ranges*, so a pin outside the affected "
        "range is never reported. This is a small built-in list — run "
        "`/secfull` for GitHub's full Dependabot data."
    )

    return "\n".join(lines)
