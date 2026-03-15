"""
Dependency Scanner - app/security/dependencies.py
V3: Scans requirements.txt / package.json for known vulnerabilities.
Uses OSV.dev API — free, no API key required.
"""

import requests
from app.core.logger import get_logger

log = get_logger(__name__)

OSV_API = "https://api.osv.dev/v1/query"


def parse_requirements(content: str) -> list[dict]:
    """Parse requirements.txt into list of {name, version} dicts."""
    packages = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ["==", ">=", "<=", "~=", "!="]:
            if sep in line:
                name, version = line.split(sep, 1)
                packages.append({"name": name.strip(), "version": version.strip().split(",")[0]})
                break
        else:
            packages.append({"name": line, "version": ""})
    return packages


def scan_package(name: str, version: str, ecosystem: str = "PyPI") -> list[dict]:
    """Query OSV.dev for vulnerabilities in a single package."""
    try:
        body = {
            "package": {"name": name, "ecosystem": ecosystem},
        }
        if version:
            body["version"] = version

        resp = requests.post(OSV_API, json=body, timeout=10)
        if resp.status_code != 200:
            return []

        vulns = resp.json().get("vulns", [])
        return [
            {
                "id": v.get("id", ""),
                "summary": v.get("summary", "No summary")[:120],
                "severity": _extract_severity(v),
            }
            for v in vulns
        ]
    except Exception as e:
        log.error("deps.scan_failed", package=name, error=str(e))
        return []


def _extract_severity(vuln: dict) -> str:
    try:
        return vuln["database_specific"]["severity"]
    except (KeyError, TypeError):
        return "UNKNOWN"


def scan_requirements_txt(content: str) -> list[dict]:
    """Scan all packages in requirements.txt. Returns findings."""
    packages = parse_requirements(content)
    findings = []
    for pkg in packages:
        vulns = scan_package(pkg["name"], pkg["version"])
        if vulns:
            findings.append({
                "package": pkg["name"],
                "version": pkg["version"],
                "vulnerabilities": vulns,
            })
            log.warning("deps.vulnerable", package=pkg["name"], count=len(vulns))
    return findings


def format_findings(findings: list[dict]) -> str:
    """Format vulnerability findings as GitHub comment."""
    if not findings:
        return "## ✅ Dependency Scan\n\nNo known vulnerabilities found!"

    lines = [
        "## ⚠️ Dependency Vulnerabilities Found\n",
        f"**{len(findings)} package(s) have known vulnerabilities.**\n",
    ]
    for f in findings:
        lines.append(f"### `{f['package']}=={f['version']}`")
        for v in f["vulnerabilities"][:3]:
            lines.append(f"- **{v['id']}** ({v['severity']}): {v['summary']}")

    lines.append("\n> Run `pip install --upgrade <package>` to update affected packages.")
    return "\n".join(lines)

