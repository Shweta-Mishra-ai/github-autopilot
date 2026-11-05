"""
app/security/dependencies.py
Dependency advisories for a requirements.txt, from GitHub's reviewed advisory
database (the one Dependabot uses), for the exact versions pinned.

It used to match pins against a hand-written table of ten advisories for
three packages, and told every other dependency of every repository it had
"no vulnerable dependencies". The table also carried this project's own
deployment constraint (a Rust build on Render) and its own risk acceptances,
both applied to every repository the bot was installed on.
"""

import re
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Only these severities open a GitHub issue on push; the rest are reported in
# /security and logged.
ISSUE_SEVERITIES: set[str] = {"HIGH", "CRITICAL"}

# GitHub's advisory severities, in the casing the rest of this module uses.
_SEVERITY = {"critical": "CRITICAL", "high": "HIGH", "medium": "MODERATE", "low": "LOW"}

# GitHub accepts up to 1,000 `affects` entries per request.
_AFFECTS_MAX = 1000


class AdvisoriesUnavailable(Exception):
    """GitHub's advisory database could not be read — so nothing can be said
    about these dependencies, clean or otherwise."""


@dataclass
class DepFinding:
    package: str
    version: str
    severity: str
    cve_id: str
    description: str
    # The advisory's vulnerable range, as GitHub states it ("< 2.4.0").
    affected_range: str = ""
    # The first release with the fix, when the advisory names one.
    fixed_in: str = ""

    @property
    def is_actionable(self) -> bool:
        """True if this finding should trigger a GitHub issue. There is no
        built-in acceptance list: one existed, holding this project's own
        "LOW risk for our use" decisions, applied to every repository."""
        return self.severity in ISSUE_SEVERITIES


# Matches `package==version`, tolerating extras (`celery[redis]==5.3.6`),
# spaces around the operator (`flask == 3.0.0`), dotted names
# (`zope.interface==6.0`) and a trailing comment or marker. Anything else —
# ranges, -r includes, VCS URLs — is skipped, because a pin is the only form
# this scanner can match against a CVE. The old pattern allowed neither spaces
# nor dots, so those pins were silently left out of the scan.
_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._\-]*)\s*(?:\[[^\]]*\])?\s*==\s*([^\s#;,]+)")

# The name at the start of any requirement line, pinned or not.
_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._\-]*)")


def _canonical(name: str) -> str:
    """PEP 503 name: `Zope_Interface` and `zope.interface` are one package."""
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_names(content: str) -> list[str]:
    """
    Every package named in a requirements file, pinned or not, in order and
    without duplicates. Options (`-r`, `-e`, `--hash`) and URLs name nothing
    this can look up and are skipped.
    """
    names: list[str] = []
    for line in (content or "").splitlines():
        line = line.split(" #", 1)[0].strip()
        if not line or line.startswith(("#", "-")) or "://" in line:
            continue
        match = _NAME_RE.match(line)
        if match and _canonical(match.group(1)) not in names:
            names.append(_canonical(match.group(1)))
    return names


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
        packages.append({"name": _canonical(match.group(1)), "version": match.group(2)})
    return packages


def unpinned_requirements(content: str) -> list[str]:
    """Packages named without an exact `==` pin: no version, so nothing to
    look up — they are reported as not checked rather than as clean."""
    pinned = {p["name"] for p in parse_requirements(content)}
    return [n for n in requirement_names(content) if n not in pinned]


def scan_requirements_txt(content: str, token: str, get=None) -> list[DepFinding]:
    """
    Every GitHub-reviewed advisory affecting the exact versions pinned in a
    requirements file. Raises AdvisoriesUnavailable when GitHub cannot be read.

    This matched pins against a hand-written table of ten entries for three
    packages — and every other package, and every repository, was then told
    "No vulnerable dependencies". GitHub's advisory database is the same one
    Dependabot uses; asking it `affects=name@version` returns exactly the
    advisories that apply, with the range and the first patched release.
    """
    if get is None:
        from app.github.client import gh_get as get
    from urllib.parse import quote

    packages = parse_requirements(content)
    if not packages:
        return []
    wanted = {p["name"]: p["version"] for p in packages}
    findings: list[DepFinding] = []
    seen: set[tuple[str, str]] = set()
    for i in range(0, len(packages), _AFFECTS_MAX):
        batch = packages[i : i + _AFFECTS_MAX]
        affects = ",".join(f"{p['name']}@{p['version']}" for p in batch)
        # A failed read is raised as itself, so a 5xx stays transient.
        advisories = get(
            f"/advisories?ecosystem=pip&per_page=100&affects={quote(affects, safe='@,')}",
            token,
        )
        if not isinstance(advisories, list):
            raise AdvisoriesUnavailable("unexpected response from the advisory database")
        for adv in advisories:
            if not isinstance(adv, dict) or adv.get("withdrawn_at"):
                continue
            ident = str(adv.get("ghsa_id") or adv.get("cve_id") or "")
            for vuln in adv.get("vulnerabilities") or []:
                pkg = _canonical(str(((vuln or {}).get("package") or {}).get("name") or ""))
                if pkg not in wanted or (pkg, ident) in seen:
                    continue
                seen.add((pkg, ident))
                patched = vuln.get("first_patched_version")
                if isinstance(patched, dict):
                    patched = patched.get("identifier")
                findings.append(
                    DepFinding(
                        package=pkg,
                        version=wanted[pkg],
                        severity=_SEVERITY.get(str(adv.get("severity", "")).lower(), "UNKNOWN"),
                        cve_id=ident,
                        description=str(adv.get("summary") or "")[:200],
                        affected_range=str(vuln.get("vulnerable_version_range") or ""),
                        fixed_in=str(patched or ""),
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
    # Catch-all. Every finding must appear somewhere: the buckets above are an
    # allow-list, so a severity nobody anticipated used to be counted and then
    # silently dropped. A security report that quietly omits a finding is worse
    # than one that renders it under an unfamiliar heading.
    known = {id(f) for f in high + moderate + low}
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
            }.get(f.severity, "⚠️")
            lines.append(f"\n`{f.package}=={f.version}`")
            lines.append(
                f"- {sev_emoji} [{f.cve_id}](https://github.com/advisories/{f.cve_id}) "
                f"({f.severity}): {f.description}"
            )
            if f.affected_range:
                fix = f"; fixed in `{f.fixed_in}`" if f.fixed_in else "; no fixed release yet"
                lines.append(f"  - Affected: `{f.affected_range}` — you have `{f.version}`{fix}")

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
    lines.append("\n---")
    lines.append("> From GitHub's reviewed advisory database, for the exact pinned versions.")

    return "\n".join(lines)
