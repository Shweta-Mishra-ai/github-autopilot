"""
Secret Detection - app/security/secrets.py
V3: Scans commit diffs for secrets before processing.
Regex + entropy based detection.
"""

import re
import math
from dataclasses import dataclass
from app.core.logger import get_logger

log = get_logger(__name__)

# Secret patterns
PATTERNS = [
    ("AWS Access Key", r"AKIA[0-9A-Z]{16}"),
    ("AWS Secret Key", r"(?i)aws.{0,20}secret.{0,20}['\"][0-9a-zA-Z/+]{40}['\"]"),
    ("GitHub Token", r"ghp_[0-9a-zA-Z]{36}"),
    ("GitHub OAuth", r"gho_[0-9a-zA-Z]{36}"),
    ("Slack Token", r"xox[baprs]-[0-9a-zA-Z\-]{10,}"),
    ("Stripe Secret", r"sk_live_[0-9a-zA-Z]{24,}"),
    ("Stripe Public", r"pk_live_[0-9a-zA-Z]{24,}"),
    ("Private Key", r"-----BEGIN (RSA |EC )?PRIVATE KEY-----"),
    (
        "Generic API Key",
        r"(?i)(api[_-]?key|apikey|api[_-]?secret).{0,10}['\"][a-zA-Z0-9_\-]{20,}['\"]",
    ),
    (
        "Generic Password",
        r"(?i)(password|passwd|pwd).{0,5}[=:].{0,5}['\"][^'\"]{8,}['\"]",
    ),
    (
        "Generic Token",
        r"(?i)(token|secret).{0,10}[=:].{0,5}['\"][a-zA-Z0-9_\-\.]{20,}['\"]",
    ),
    ("Groq API Key", r"gsk_[0-9a-zA-Z]{50,}"),
]

HIGH_ENTROPY_THRESHOLD = 4.5
MIN_LENGTH_FOR_ENTROPY = 20


@dataclass
class SecretFinding:
    pattern_name: str
    line_number: int
    severity: str  # high / medium
    redacted_match: str


def _entropy(s: str) -> float:
    """Shannon entropy of a string."""
    if not s:
        return 0.0
    freq = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    return -sum((f / len(s)) * math.log2(f / len(s)) for f in freq.values())


def scan_diff(diff: str) -> list[SecretFinding]:
    """Scan a git diff string for secrets. Returns list of findings."""
    findings = []
    lines = diff.splitlines()

    for lineno, line in enumerate(lines, 1):
        # Only scan added lines
        if not line.startswith("+"):
            continue

        content = line[1:]  # Remove leading +

        # Pattern matching
        for name, pattern in PATTERNS:
            match = re.search(pattern, content)
            if match:
                matched = match.group(0)
                redacted = (
                    matched[:6] + "..." + matched[-4:] if len(matched) > 12 else "***"
                )
                findings.append(
                    SecretFinding(
                        pattern_name=name,
                        line_number=lineno,
                        severity="high",
                        redacted_match=redacted,
                    )
                )
                log.warning("secret.detected", pattern=name, line=lineno)

        # Entropy check for long strings
        tokens = re.findall(r"['\"][a-zA-Z0-9+/=_\-]{20,}['\"]", content)
        for token in tokens:
            clean = token.strip("'\"")
            if _entropy(clean) > HIGH_ENTROPY_THRESHOLD:
                findings.append(
                    SecretFinding(
                        pattern_name="High Entropy String",
                        line_number=lineno,
                        severity="medium",
                        redacted_match=clean[:6] + "...",
                    )
                )

    return findings


def format_findings(findings: list[SecretFinding], repo: str) -> str:
    """Format findings as a GitHub comment."""
    if not findings:
        return ""

    lines = [
        "## 🚨 Secret Detection Alert\n",
        f"**{len(findings)} potential secret(s) detected in this push.**\n",
        "| Line | Type | Severity | Redacted Match |",
        "|------|------|----------|----------------|",
    ]
    for f in findings:
        lines.append(
            f"| {f.line_number} | {f.pattern_name} | `{f.severity}` | `{f.redacted_match}` |"
        )

    lines += [
        "\n⚠️ **Action required:** Remove the secret, rotate the credential, and force-push.",
        "\n> 🔒 Never commit secrets. Use environment variables or a secrets manager.",
    ]

    return "\n".join(lines)
