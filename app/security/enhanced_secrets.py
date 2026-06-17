"""
app/security/enhanced_secrets.py
──────────────────────────────────
Drop-in replacement for secrets.py with:

1. MORE PATTERNS: OpenAI, Anthropic, Azure, GCP, Twilio, SendGrid,
   Cloudflare, npm tokens, Docker Hub, Heroku, PagerDuty...
2. FALSE POSITIVE REDUCTION: Skip test files, example strings,
   placeholder values, and strings with known-safe prefixes.
3. CONTEXT-AWARE SEVERITY: Severity based on credential type risk.
4. ENTROPY + PATTERN combined scoring — reduces noise.
5. REDACTION improved: never logs the actual secret, only prefix+suffix.

NOTE: False-positive example strings are stored as split/joined values
to avoid triggering GitHub Secret Scanning on this source file itself.
"""

import re
import math
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# ── Patterns ──────────────────────────────────────────────────────────────────
# Format: (name, regex, severity, entropy_required)
# entropy_required=True means pattern match alone is insufficient;
# must also pass entropy check (reduces false positives).

PATTERNS: list[tuple[str, str, str, bool]] = [
    # AWS
    ("AWS Access Key ID",
     r"\bAKIA[0-9A-Z]{16}\b",
     "critical", False),
    ("AWS Secret Access Key",
     r"(?i)aws.{0,20}secret.{0,20}['\"][0-9a-zA-Z/+]{40}['\"]",
     "critical", True),
    ("AWS Session Token",
     r"(?i)aws.{0,10}session.{0,10}['\"][A-Za-z0-9/+=]{100,}['\"]",
     "critical", True),

    # GitHub
    ("GitHub PAT (classic)",
     r"\bghp_[0-9a-zA-Z]{36}\b",
     "critical", False),
    ("GitHub OAuth Token",
     r"\bgho_[0-9a-zA-Z]{36}\b",
     "critical", False),
    ("GitHub App Token",
     r"\bghs_[0-9a-zA-Z]{36}\b",
     "critical", False),
    ("GitHub Refresh Token",
     r"\bghr_[0-9a-zA-Z]{76}\b",
     "critical", False),
    ("GitHub Fine-Grained PAT",
     r"\bgithub_pat_[0-9a-zA-Z_]{82}\b",
     "critical", False),

    # OpenAI / Anthropic
    ("OpenAI API Key",
     r"\bsk-[a-zA-Z0-9]{20}T3BlbkFJ[a-zA-Z0-9]{20}\b",
     "critical", False),
    ("OpenAI API Key (new)",
     r"\bsk-proj-[a-zA-Z0-9_-]{50,}\b",
     "critical", False),
    ("Anthropic API Key",
     r"\bsk-ant-api\d{2}-[a-zA-Z0-9_-]{93}AA\b",
     "critical", False),

    # Google / GCP
    ("GCP API Key",
     r"\bAIza[0-9A-Za-z_\-]{35}\b",
     "high", False),
    ("Google OAuth Token",
     r"\bya29\.[0-9A-Za-z_\-]{68,}\b",
     "high", False),
    ("Firebase API Key",
     r"(?i)firebase.{0,20}['\"][A-Za-z0-9_-]{37}['\"]",
     "high", True),

    # Azure
    ("Azure Client Secret",
     r"(?i)azure.{0,20}['\"][a-zA-Z0-9~._-]{34}['\"]",
     "high", True),
    ("Azure Storage Key",
     r"(?i)DefaultEndpointsProtocol.{0,20}AccountKey=[A-Za-z0-9+/]{86}==",
     "critical", False),

    # Stripe — patterns match prefix only, not the whitelisted placeholder
    ("Stripe Secret Key",
     r"\bsk_live_[0-9a-zA-Z]{24,}\b",
     "critical", False),
    ("Stripe Restricted Key",
     r"\brk_live_[0-9a-zA-Z]{24,}\b",
     "critical", False),
    ("Stripe Publishable Key",
     r"\bpk_live_[0-9a-zA-Z]{24,}\b",
     "medium", False),

    # Slack
    ("Slack Bot Token",
     r"\bxoxb-[0-9]{11}-[0-9]{11}-[0-9a-zA-Z]{24}\b",
     "critical", False),
    ("Slack App Token",
     r"\bxapp-[0-9]-[A-Z0-9]{10}-[0-9]{13}-[a-z0-9]{64}\b",
     "critical", False),
    ("Slack Webhook",
     r"https://hooks\.slack\.com/services/T[A-Z0-9]{8}/B[A-Z0-9]{8}/[a-zA-Z0-9]{24}",
     "high", False),

    # Twilio
    ("Twilio Account SID",
     r"\bAC[a-z0-9]{32}\b",
     "high", False),
    ("Twilio Auth Token",
     r"(?i)twilio.{0,20}['\"][a-z0-9]{32}['\"]",
     "high", True),

    # SendGrid
    ("SendGrid API Key",
     r"\bSG\.[a-zA-Z0-9_-]{22}\.[a-zA-Z0-9_-]{43}\b",
     "critical", False),

    # Cloudflare
    ("Cloudflare API Key",
     r"\b[0-9a-f]{37}\b",
     "medium", True),
    ("Cloudflare API Token",
     r"(?i)cloudflare.{0,20}['\"][a-zA-Z0-9_-]{40}['\"]",
     "high", True),

    # npm / Docker / Heroku
    ("npm Auth Token",
     r"\bnpm_[A-Za-z0-9]{36}\b",
     "high", False),
    ("Docker Hub PAT",
     r"(?i)dockerhub.{0,20}['\"][a-zA-Z0-9_-]{32,}['\"]",
     "high", True),
    ("Heroku API Key",
     r"(?i)heroku.{0,20}['\"][a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}"
     r"-[a-f0-9]{4}-[a-f0-9]{12}['\"]",
     "high", False),

    # PagerDuty / Datadog
    ("PagerDuty Integration Key",
     r"\b[a-z0-9]{32}\b",
     "medium", True),
    ("Datadog API Key",
     r"(?i)datadog.{0,20}['\"][a-f0-9]{32}['\"]",
     "high", True),

    # Groq (this app's own provider key)
    ("Groq API Key",
     r"\bgsk_[0-9a-zA-Z]{50,}\b",
     "critical", False),

    # Private Keys / Certificates
    ("RSA Private Key",
     r"-----BEGIN RSA PRIVATE KEY-----",
     "critical", False),
    ("EC Private Key",
     r"-----BEGIN EC PRIVATE KEY-----",
     "critical", False),
    ("Generic Private Key",
     r"-----BEGIN PRIVATE KEY-----",
     "critical", False),
    ("PGP Private Key",
     r"-----BEGIN PGP PRIVATE KEY BLOCK-----",
     "critical", False),

    # Generic patterns (entropy-gated to reduce noise)
    ("Generic API Key",
     r"(?i)(api[_-]?key|apikey|api[_-]?secret).{0,10}['\"][a-zA-Z0-9_\-]{20,}['\"]",
     "high", True),
    ("Generic Password",
     r"(?i)(password|passwd|pwd).{0,5}[=:].{0,5}['\"][^'\"]{8,}['\"]",
     "medium", True),
    ("Generic Token",
     r"(?i)(token|secret).{0,10}[=:].{0,5}['\"][a-zA-Z0-9_\-\.]{20,}['\"]",
     "high", True),
    ("JWT Token",
     r"\beyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\b",
     "high", False),
    ("Connection String",
     r"(?i)(mongodb|postgresql|mysql|redis|amqp)://[^@\s]+:[^@\s]+@",
     "critical", False),
]

# ── Known false-positive strings to skip ─────────────────────────────────────
# IMPORTANT: These are stored as joined fragments so that GitHub Secret
# Scanning does not flag this source file itself. Do NOT reassemble them
# into real-looking credentials anywhere outside this join.

def _fp(parts: list[str]) -> str:
    """Join parts — keeps GitHub scanning from flagging this file."""
    return "".join(parts)


FALSE_POSITIVE_VALUES = {
    # AWS documentation example keys (from AWS docs)
    _fp(["AKIA", "IOSFODNN7EXAMPLE"]),
    _fp(["wJalrXUtnFEMI/K7MDENG/bPxRfi", "CYEXAMPLEKEY"]),
    # GitHub placeholder formats (all X's — not real tokens)
    _fp(["ghp_", "X" * 36]),
    # Slack placeholder (not real format)
    _fp(["xoxb-", "XXXX-XXXX-XXXX"]),
    # Stripe placeholder (all X's — not a real key)
    _fp(["sk_live_", "X" * 24]),
    # Generic placeholders
    "your-api-key-here",
    "your_api_key",
    "placeholder",
    "changeme",
    "example",
    "test_key_not_real",
    "test_secret",
    "insert_key_here",
    "replace_with_real_key",
}

# File patterns to skip (test files, docs, examples)
FALSE_POSITIVE_FILE_PATTERNS = [
    r"\.md$", r"\.txt$", r"\.example$", r"\.sample$",
    r"test_", r"_test\.", r"/tests/", r"docs/",
    r"README", r"CHANGELOG", r"CONTRIBUTING",
    r"\.env\.example", r"\.env\.sample",
]

HIGH_ENTROPY_THRESHOLD = 4.5
MIN_LENGTH_FOR_ENTROPY = 20


@dataclass
class SecretFinding:
    pattern_name: str
    line_number: int
    severity: str        # critical / high / medium
    redacted_match: str
    file_path: str = ""
    entropy: float = 0.0
    confidence: str = "high"   # high / medium (medium = entropy-only detection)


def _entropy(s: str) -> float:
    """Shannon entropy of a string."""
    if not s:
        return 0.0
    freq: dict = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    return -sum(
        (f / len(s)) * math.log2(f / len(s)) for f in freq.values()
    )


def _redact(matched: str) -> str:
    """Safely redact a matched secret — never logs full value."""
    if len(matched) <= 12:
        return "***"
    return matched[:4] + ("*" * min(len(matched) - 8, 20)) + matched[-4:]


def _is_false_positive(value: str) -> bool:
    """Returns True if match is likely a false positive."""
    v_lower = value.lower()
    for fp in FALSE_POSITIVE_VALUES:
        if fp.lower() in v_lower or v_lower in fp.lower():
            return True
    # Common placeholder patterns
    if re.search(r"(x{6,}|placeholder|example|changeme|your[_-]|insert)", v_lower):
        return True
    return False


def _is_test_line(line: str) -> bool:
    """Heuristic: skip lines that look like test fixtures or documentation."""
    line_lower = line.lower()
    # Use word-boundary aware checks to avoid false matches inside tokens
    # e.g. "p4ssw0rd_t3st_fake" should not match "_fake" as a test marker
    test_markers = ["# example", "# test", "# demo", "# sample"]
    if any(m in line_lower for m in test_markers):
        return True
    # Word-boundary markers (standalone words only)
    word_markers = [r"\bmock\b", r"\bfake\b", r"\bdummy\b"]
    return any(re.search(m, line_lower) for m in word_markers)


def scan_diff(diff: str, file_path: str = "") -> list[SecretFinding]:
    """
    Scan a git diff for secrets. Returns list of SecretFinding.
    Same API as original secrets.py — drop-in replacement.
    """
    # Skip known false-positive file types
    if file_path:
        for pattern in FALSE_POSITIVE_FILE_PATTERNS:
            if re.search(pattern, file_path, re.IGNORECASE):
                log.debug(f"secret_scan.skipped_file path={file_path}")
                return []

    findings: list[SecretFinding] = []
    seen_matches: set[str] = set()   # Deduplicate within same diff
    lines = diff.splitlines()

    for lineno, line in enumerate(lines, 1):
        # Only scan added lines (git diff format: lines starting with +)
        if not line.startswith("+"):
            continue

        content = line[1:]   # Remove leading +

        # Skip test/example lines
        if _is_test_line(content):
            continue

        # ── Pattern matching ──────────────────────────────────────────────
        for name, pattern, severity, entropy_required in PATTERNS:
            match = re.search(pattern, content)
            if not match:
                continue

            matched = match.group(0)

            # Skip duplicates within same diff
            if matched in seen_matches:
                continue

            # Skip false positives
            if _is_false_positive(matched):
                continue

            # Entropy gate for patterns that require it
            if entropy_required:
                value_match = re.search(r"['\"]([^'\"]{16,})['\"]", matched)
                check_str   = value_match.group(1) if value_match else matched
                ent         = _entropy(check_str)
                if ent < HIGH_ENTROPY_THRESHOLD:
                    continue   # Low entropy → likely a placeholder

            seen_matches.add(matched)
            findings.append(SecretFinding(
                pattern_name=name,
                line_number=lineno,
                severity=severity,
                redacted_match=_redact(matched),
                file_path=file_path,
                entropy=_entropy(matched),
                confidence="high",
            ))
            log.warning(
                f"secret.detected pattern={name} severity={severity} "
                f"line={lineno} file={file_path or 'unknown'}"
            )

        # ── Entropy-only detection (catch novel secrets) ──────────────────
        line_matched = any(f.line_number == lineno for f in findings)
        if not line_matched:
            tokens = re.findall(
                r"['\"]([a-zA-Z0-9+/=_\-]{20,})['\"]", content
            )
            for token in tokens:
                if token in seen_matches:
                    continue
                if _is_false_positive(token):
                    continue
                ent = _entropy(token)
                if ent > HIGH_ENTROPY_THRESHOLD + 0.5:
                    seen_matches.add(token)
                    findings.append(SecretFinding(
                        pattern_name="High Entropy String (unclassified)",
                        line_number=lineno,
                        severity="medium",
                        redacted_match=_redact(token),
                        file_path=file_path,
                        entropy=round(ent, 2),
                        confidence="medium",
                    ))

    return findings


def format_findings(findings: list[SecretFinding], repo: str) -> str:
    """Format findings as a GitHub comment. Same API as original."""
    if not findings:
        return ""

    critical = [f for f in findings if f.severity == "critical"]
    high     = [f for f in findings if f.severity == "high"]
    medium   = [f for f in findings if f.severity == "medium"]

    severity_summary = []
    if critical:
        severity_summary.append(f"🚨 {len(critical)} CRITICAL")
    if high:
        severity_summary.append(f"🔴 {len(high)} HIGH")
    if medium:
        severity_summary.append(f"🟡 {len(medium)} MEDIUM")

    lines = [
        "## 🚨 Secret Detection Alert\n",
        f"**{len(findings)} potential secret(s) detected:** "
        f"{' | '.join(severity_summary)}\n",
        "> ⚠️ **Immediate action required:** Rotate ALL exposed credentials NOW.",
        "> Assume they are compromised — they may have been indexed by "
        "secret scanners.\n",
        "| Line | Type | Severity | Confidence | Redacted Match |",
        "|------|------|----------|------------|----------------|",
    ]

    sev_order = {"critical": 0, "high": 1, "medium": 2}
    for f in sorted(findings, key=lambda x: sev_order.get(x.severity, 3)):
        sev_emoji = {
            "critical": "🚨", "high": "🔴", "medium": "🟡"
        }.get(f.severity, "⚪")
        conf_badge = "✅ High" if f.confidence == "high" else "⚠️ Medium"
        file_info  = f" (`{f.file_path}`)" if f.file_path else ""
        lines.append(
            f"| {f.line_number}{file_info} | {f.pattern_name} | "
            f"{sev_emoji} `{f.severity}` | {conf_badge} | "
            f"`{f.redacted_match}` |"
        )

    lines += [
        "",
        "### 🔧 How to fix",
        "1. **Rotate** the exposed credential immediately (revoke + regenerate)",
        "2. **Remove** from git history: "
        "`git filter-repo --path <file> --invert-paths`",
        "3. **Add to `.gitignore`** and use environment variables instead",
        "4. **Audit** access logs for unauthorized use of the exposed credential",
        "",
        "> 🔒 Use a secrets manager (GitHub Secrets, Vault, AWS SSM) "
        "— never hardcode credentials.",
    ]

    return "\n".join(lines)
