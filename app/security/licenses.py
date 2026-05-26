"""
License Scanner - app/security/licenses.py
V3: Check dependency licenses for compliance.
Flags copyleft licenses in permissive projects.
"""

import requests
from app.core.logger import get_logger

log = get_logger(__name__)

PYPI_API = "https://pypi.org/pypi/{package}/json"

# Permissive licenses - safe for all projects
PERMISSIVE = {
    "MIT",
    "MIT License",
    "Apache-2.0",
    "Apache Software License",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "BSD License",
    "ISC",
    "ISC License",
    "Python Software Foundation License",
    "Public Domain",
    "Unlicense",
}

# Copyleft licenses - may have restrictions
COPYLEFT = {
    "GPL-2.0",
    "GPL-3.0",
    "GNU General Public License v2",
    "GNU General Public License v3",
    "GPLv2",
    "GPLv3",
    "LGPL-2.0",
    "LGPL-2.1",
    "LGPL-3.0",
    "AGPL-3.0",
    "GNU Affero General Public License v3",
    "MPL-2.0",
    "Mozilla Public License 2.0",
}


def check_package_license(package: str) -> dict:
    """Get license info for a PyPI package."""
    try:
        resp = requests.get(PYPI_API.format(package=package), timeout=5)
        if resp.status_code != 200:
            return {"package": package, "license": "Unknown", "risk": "unknown"}

        info = resp.json().get("info", {})
        license_str = info.get("license", "") or ""

        # Determine risk
        risk = "unknown"
        for lic in PERMISSIVE:
            if lic.lower() in license_str.lower():
                risk = "safe"
                break
        if risk == "unknown":
            for lic in COPYLEFT:
                if lic.lower() in license_str.lower():
                    risk = "copyleft"
                    break

        return {
            "package": package,
            "license": license_str[:80] or "Not specified",
            "risk": risk,
        }

    except Exception as e:
        log.error("licenses.check_failed", package=package, error=str(e))
        return {"package": package, "license": "Error", "risk": "unknown"}


def scan_requirements(content: str) -> list[dict]:
    """Scan requirements.txt and check all package licenses."""
    from app.security.dependencies import parse_requirements

    packages = parse_requirements(content)
    results = []
    for pkg in packages[:20]:  # Limit to 20 to avoid rate limits
        result = check_package_license(pkg["name"])
        if result["risk"] in ("copyleft", "unknown"):
            results.append(result)
    return results


def format_findings(findings: list[dict]) -> str:
    if not findings:
        return "## ✅ License Scan\n\nAll dependencies use permissive licenses."

    lines = [
        "## ⚖️ License Compliance\n",
        f"**{len(findings)} package(s) have restrictive or unknown licenses.**\n",
        "| Package | License | Risk |",
        "|---------|---------|------|",
    ]
    for f in findings:
        emoji = "🔴" if f["risk"] == "copyleft" else "🟡"
        lines.append(f"| `{f['package']}` | {f['license']} | {emoji} `{f['risk']}` |")

    lines.append(
        "\n> Review copyleft licenses carefully — they may require you to open-source your code."
    )
    return "\n".join(lines)
