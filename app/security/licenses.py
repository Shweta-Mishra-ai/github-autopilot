"""
License Scanner - app/security/licenses.py
Flag copyleft dependencies in a permissively-licensed project.

READING THE METADATA CORRECTLY IS THE WHOLE PROBLEM
  This scanner used to read `info.license` and nothing else. Six of this
  repository's own eight direct dependencies leave that field EMPTY — Flask,
  redis, gunicorn, cryptography, PyJWT and structlog all declare their licence
  in `license_expression` (PEP 639) or in trove classifiers instead. Every one
  of them was reported as "unknown", i.e. as something for a maintainer to go
  and check. A scanner that is wrong about three quarters of a normal
  requirements file is worse than no scanner: it trains people to skim past it.

  So all three sources are read, in order of authority:
    1. license_expression  — SPDX, machine-readable, unambiguous
    2. license             — free text, often an SPDX id, often empty
    3. classifiers         — "License :: OSI Approved :: MIT License"

MATCHING BY TOKEN, NOT BY SUBSTRING
  The old check asked whether any known name appeared anywhere in the string.
  "MIT AND GPL-3.0" contains "MIT", so a dual-licence package that genuinely
  imposes the GPL was reported as safe — a false NEGATIVE in the one direction
  that matters for compliance. Expressions are parsed instead:

    OR   the consumer may choose  -> the most permissive branch wins
    AND  every licence applies    -> the most restrictive branch wins
"""

import re

import requests

from app.core.logger import EventLogger

log = EventLogger("licenses")

PYPI_API = "https://pypi.org/pypi/{package}/json"

# Normalised identifiers, matched exactly after _normalise(). Keeping these as
# canonical ids rather than prose is what makes exact matching possible.
PERMISSIVE = {
    "mit",
    "mit-0",
    "apache-2.0",
    "apache software license",
    "apache license",
    "bsd",
    "bsd-2-clause",
    "bsd-3-clause",
    "bsd license",
    "0bsd",
    "isc",
    "psf-2.0",
    "python software foundation license",
    "public domain",
    "unlicense",
    "cc0-1.0",
    "zlib",
    "wtfpl",
}

# Reciprocal licences. MPL and LGPL are file-/library-scoped rather than
# project-scoped, but "may have restrictions" is exactly what this report is
# for — the maintainer decides, the scanner only surfaces.
COPYLEFT = {
    "gpl",
    "gpl-2.0",
    "gpl-3.0",
    "gplv2",
    "gplv3",
    "agpl-3.0",
    "agplv3",
    "lgpl-2.0",
    "lgpl-2.1",
    "lgpl-3.0",
    "lgplv3",
    "mpl-1.1",
    "mpl-2.0",
    "epl-1.0",
    "epl-2.0",
    "cddl-1.0",
    "osl-3.0",
    "sspl-1.0",
    "gnu general public license",
    "gnu affero general public license",
    "gnu lesser general public license",
    "mozilla public license",
    "eclipse public license",
}

# "GPL-3.0-or-later", "GPL-3.0+", "Apache-2.0 WITH LLVM-exception" all reduce to
# the base identifier for classification purposes.
_SUFFIX_RE = re.compile(r"[-\s](?:or[-\s]later|only)$|\+$", re.I)
_WITH_RE = re.compile(r"\s+with\s+.*$", re.I)
_VERSION_WORDS = re.compile(r"\s+v(\d)", re.I)

_RISK_ORDER = {"safe": 0, "unknown": 1, "copyleft": 2}


def _normalise(token: str) -> str:
    """One licence identifier, reduced to a canonical lowercase form."""
    token = token.strip().strip("()").strip()
    token = _WITH_RE.sub("", token)
    token = _SUFFIX_RE.sub("", token)
    token = _VERSION_WORDS.sub(r" v\1", token)
    token = re.sub(r"\s+", " ", token).strip().lower()
    # "MIT License" and "MIT" are the same licence.
    if token.endswith(" license") and token[:-8] in PERMISSIVE | COPYLEFT:
        token = token[:-8]
    return token


def _classify_identifier(token: str) -> str:
    """safe | copyleft | unknown for a single identifier."""
    norm = _normalise(token)
    if not norm:
        return "unknown"
    if norm in PERMISSIVE:
        return "safe"
    if norm in COPYLEFT:
        return "copyleft"
    # A versioned variant of a known family: "gpl-3.0-linking-exception".
    for known in COPYLEFT:
        if norm.startswith(known + "-") or norm.startswith(known + " "):
            return "copyleft"
    for known in PERMISSIVE:
        if norm.startswith(known + "-") or norm.startswith(known + " "):
            return "safe"
    return "unknown"


def _split_top_level(expr: str, operator: str) -> list[str]:
    """
    Split on `operator` only where it is NOT inside parentheses.

    A naive re.split gets "GPL-3.0 AND (MIT OR Apache-2.0)" exactly backwards:
    it cuts at the inner OR, yielding "GPL-3.0 AND (MIT" and "Apache-2.0)",
    then takes the permissive branch and calls the whole thing safe. The GPL
    term is mandatory — it is outside the parentheses.
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    tokens = re.split(r"(\s+|\(|\))", expr)

    for tok in tokens:
        if tok == "(":
            depth += 1
            current.append(tok)
        elif tok == ")":
            depth -= 1
            current.append(tok)
        elif depth == 0 and tok.strip().lower() == operator:
            parts.append("".join(current))
            current = []
        else:
            current.append(tok)

    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _strip_outer_parens(expr: str) -> str:
    """`(MIT OR Apache-2.0)` -> `MIT OR Apache-2.0`, only when balanced."""
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        depth = 0
        for i, ch in enumerate(expr):
            depth += (ch == "(") - (ch == ")")
            if depth == 0 and i < len(expr) - 1:
                return expr  # the parens are not a single enclosing pair
        expr = expr[1:-1].strip()
    return expr


def classify_expression(expr: str) -> str:
    """
    Classify an SPDX-style expression.

    OR  — the consumer may pick, so the most permissive branch decides.
    AND — every licence applies, so the most restrictive branch decides.

    Substring matching got this backwards: "MIT AND GPL-3.0" contains "MIT" and
    was reported safe, which is the false negative that actually costs someone
    a licence violation.

    OR binds loosest, matching SPDX, so it is split first — but only at the top
    level (see _split_top_level).
    """
    if not expr or not expr.strip():
        return "unknown"

    expr = _strip_outer_parens(expr)

    or_parts = _split_top_level(expr, "or")
    if len(or_parts) > 1:
        risks = [classify_expression(part) for part in or_parts]
        return min(risks, key=lambda r: _RISK_ORDER[r])

    and_parts = _split_top_level(expr, "and")
    if len(and_parts) > 1:
        risks = [classify_expression(part) for part in and_parts]
        return max(risks, key=lambda r: _RISK_ORDER[r])

    return _classify_identifier(expr)


def _classifier_licences(classifiers: list) -> list[str]:
    """Licence names out of trove classifiers, most specific segment only."""
    out = []
    for c in classifiers or []:
        if not isinstance(c, str) or not c.startswith("License ::"):
            continue
        name = c.split("::")[-1].strip()
        # "OSI Approved" alone names no licence.
        if name and name.lower() not in ("osi approved", "other/proprietary license"):
            out.append(name)
    return out


def license_from_metadata(info: dict) -> tuple[str, str]:
    """
    (display string, risk) from a PyPI `info` object.

    Order is authority order, not convenience: license_expression is SPDX and
    unambiguous, `license` is free text, classifiers are a coarse taxonomy.
    """
    expr = (info.get("license_expression") or "").strip()
    if expr:
        return expr, classify_expression(expr)

    raw = (info.get("license") or "").strip()
    if raw and len(raw) < 200:  # a full licence TEXT pasted into the field is not an id
        risk = classify_expression(raw)
        if risk != "unknown":
            return raw, risk

    names = _classifier_licences(info.get("classifiers", []))
    if names:
        # Several classifiers mean "any of these", which is an OR.
        risks = [classify_expression(n) for n in names]
        return " OR ".join(names), min(risks, key=lambda r: _RISK_ORDER[r])

    if raw:
        return raw[:80], "unknown"
    return "Not specified", "unknown"


def check_package_license(package: str) -> dict:
    """
    Licence and risk for one PyPI package.

    `risk` is one of:
      safe        a permissive licence
      copyleft    reciprocal terms the maintainer should look at
      unknown     PyPI has the package but declares no licence anywhere
      unchecked   we could not ask (404, network, malformed response)

    "unchecked" is separate from "unknown" on purpose. A private or git
    dependency is not on PyPI at all, and reporting it as a licence risk is a
    false positive about a package this scanner never saw.
    """
    result = {"package": package, "license": "", "risk": "unchecked"}
    try:
        resp = requests.get(PYPI_API.format(package=package), timeout=5)
        if resp.status_code == 404:
            result["license"] = "not on PyPI"
            return result
        if resp.status_code != 200:
            result["license"] = f"PyPI returned {resp.status_code}"
            return result

        info = resp.json().get("info", {})
        if not isinstance(info, dict):
            result["license"] = "malformed PyPI response"
            return result

        display, risk = license_from_metadata(info)
        result["license"] = display[:80]
        result["risk"] = risk
        return result

    except Exception as e:
        log.error("licenses.check_failed", package=package, error=str(e))
        result["license"] = "lookup failed"
        return result


# One PyPI lookup per package, serially, with a 5s socket timeout. Twenty of
# them is a 100-second worst case inside a webhook handler, so the loop is
# bounded by wall-clock as well as by count: whichever limit is reached first
# stops the scan, and the partial result is still worth reporting.
MAX_PACKAGES = 20
SCAN_DEADLINE_SECONDS = 20.0


def scan_requirements(content: str) -> list[dict]:
    """
    Scan requirements.txt and report packages that are copyleft or unknown.

    Best-effort by design: a package that could not be checked before the
    deadline is simply absent from the report rather than reported as risky.
    Guessing "unknown" for a package we never asked about would be a false
    positive, and this scanner exists to be trusted.
    """
    import time

    from app.security.dependencies import parse_requirements

    packages = parse_requirements(content)
    results = []
    deadline = time.monotonic() + SCAN_DEADLINE_SECONDS
    for pkg in packages[:MAX_PACKAGES]:
        if time.monotonic() >= deadline:
            log.info("licenses.scan_truncated", checked=len(results))
            break
        result = check_package_license(pkg["name"])
        # "unchecked" is excluded deliberately: a package this scanner could
        # not reach — a private index, a git dependency, a network blip — is
        # not evidence of a licence problem, and reporting it as one is the
        # false positive that makes the whole section ignorable.
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
