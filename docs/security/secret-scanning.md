# Secret Scanning

> How GitHub Autopilot detects leaked credentials in code commits.
> Pattern design, entropy gating, false-positive suppression, deduplication, and known limitations.

---

## Table of Contents

1. [Overview](#1-overview)
2. [When Scanning Runs](#2-when-scanning-runs)
3. [Detection Architecture](#3-detection-architecture)
4. [Pattern Categories](#4-pattern-categories)
5. [Entropy Gating](#5-entropy-gating)
6. [False Positive Suppression](#6-false-positive-suppression)
7. [Alert Deduplication](#7-alert-deduplication)
8. [Severity Classification](#8-severity-classification)
9. [Alert Format](#9-alert-format)
10. [Source File Safety](#10-source-file-safety)
11. [Known Limitations](#11-known-limitations)
12. [Comparison to GitHub Native Secret Scanning](#12-comparison-to-github-native-secret-scanning)

---

## 1. Overview

The secret scanner (`app/security/enhanced_secrets.py`) analyses git diff output for credential patterns on every push. It replaced the original `secrets.py` in v4 with:

- **35+ patterns** covering all major credential categories
- **Entropy gating** on ambiguous patterns — reduces false positives significantly
- **False positive suppression** — whitelisted example strings, test file skipping, placeholder detection
- **Alert deduplication** — same credential set generates exactly one GitHub issue per hour
- **No scannable literals in source** — the scanner source cannot trigger GitHub Secret Scanning

---

## 2. When Scanning Runs

### On every push (`push.handle()`)

```python
def _scan_secrets(repo: str, commits: list, token: str, config, log):
    for commit in commits:
        commit_detail = gh_get(f"/repos/{repo}/commits/{commit['id']}", token)
        for file_info in commit_detail.get("files", []):
            patch    = file_info.get("patch", "")
            filename = file_info.get("filename", "")
            if patch:
                findings = scan_diff(patch, file_path=filename)
                if findings:
                    key = _findings_dedup_key(findings)
                    if not _already_reported(repo, key):
                        _create_secret_issue(repo, findings, token, config)
                        notify_secret_detected(repo, findings)
```

### On PR security scan (`/security` command)

`scan_diff()` also runs on the PR diff when `/security` is posted. This provides on-demand scanning before merging, in addition to automatic push-time scanning.

### What is NOT scanned

- Removed lines (`-` prefix in diff) — secrets being deleted are not alerts
- Context lines (no `+` or `-` prefix) — unchanged code
- Test files, markdown, `.env.example` — automatically skipped by file path
- Lines identified as test fixtures or documentation examples

---

## 3. Detection Architecture

```python
def scan_diff(diff: str, file_path: str = "") -> list[SecretFinding]:

    # Step 1: Skip known false-positive file types
    if file_path and _is_skipped_file(file_path):
        return []

    findings     = []
    seen_matches = set()   # dedup within same diff

    for lineno, line in enumerate(diff.splitlines(), 1):

        # Step 2: Only scan added lines (+ prefix in git diff)
        if not line.startswith("+"):
            continue
        content = line[1:]   # remove the leading +

        # Step 3: Skip test fixture lines
        if _is_test_line(content):
            continue

        # Step 4: Pattern matching
        for name, pattern, severity, entropy_required in PATTERNS:
            match = re.search(pattern, content)
            if not match:
                continue
            matched = match.group(0)

            if matched in seen_matches:
                continue   # already found in this diff

            if _is_false_positive(matched):
                continue   # whitelisted or placeholder

            # Step 5: Entropy gate (for ambiguous patterns only)
            if entropy_required:
                value = _extract_quoted_value(matched)
                if _entropy(value) < HIGH_ENTROPY_THRESHOLD:
                    continue   # low entropy → likely placeholder

            seen_matches.add(matched)
            findings.append(SecretFinding(...))

        # Step 6: Entropy-only detection (novel formats)
        if not any(f.line_number == lineno for f in findings):
            for token in re.findall(r"['\"]([a-zA-Z0-9+/=_\-]{20,})['\"]", content):
                if _entropy(token) > HIGH_ENTROPY_THRESHOLD + 0.5:
                    findings.append(SecretFinding(
                        pattern_name="High Entropy String (unclassified)",
                        severity="medium",
                        confidence="medium",
                    ))

    return findings
```

The six-step pipeline ensures only genuinely suspicious added lines generate findings, and only when they pass both pattern matching and optional entropy validation.

---

## 4. Pattern Categories

All 35+ patterns, grouped by category. Pattern prefixes are written as descriptions rather than literals to keep this documentation file safe from GitHub Secret Scanning.

### Cloud Providers

| Name | Pattern prefix | Severity | Entropy required |
|------|---------------|----------|-----------------|
| AWS Access Key ID | `AKIA` + 16 uppercase alphanumerics | Critical | No |
| AWS Secret Access Key | `aws` + `secret` + quoted 40-char value | Critical | Yes |
| AWS Session Token | `aws` + `session` + quoted 100+ char value | Critical | Yes |
| GCP API Key | `AIza` + 35 alphanumerics | High | No |
| Google OAuth Token | `ya29.` + 68+ alphanumerics | High | No |
| Firebase API Key | `firebase` + quoted 37-char value | High | Yes |
| Azure Client Secret | `azure` + quoted 34-char value | High | Yes |
| Azure Storage Key | `DefaultEndpointsProtocol` + `AccountKey=` + 86 base64 chars + `==` | Critical | No |

### AI API Keys

| Name | Pattern prefix | Severity | Entropy required |
|------|---------------|----------|-----------------|
| OpenAI (classic) | `sk-` + 20 chars + `T3BlbkFJ` + 20 chars | Critical | No |
| OpenAI (new format) | `sk-proj-` + 50+ alphanumerics | Critical | No |
| Anthropic | `sk-ant-api` + 2 digits + `-` + 93 chars + `AA` | Critical | No |
| Groq | `gsk_` + 50+ alphanumerics | Critical | No |

### Version Control

| Name | Pattern prefix | Severity |
|------|---------------|----------|
| GitHub PAT (classic) | `ghp_` + 36 alphanumerics | Critical |
| GitHub OAuth Token | `gho_` + 36 alphanumerics | Critical |
| GitHub App Token | `ghs_` + 36 alphanumerics | Critical |
| GitHub Refresh Token | `ghr_` + 76 alphanumerics | Critical |
| GitHub Fine-Grained PAT | `github_pat_` + 82 alphanumerics | Critical |

### Payment

| Name | Pattern prefix | Severity |
|------|---------------|----------|
| Stripe Secret Key | `sk_live_` + 24+ alphanumerics | Critical |
| Stripe Restricted Key | `rk_live_` + 24+ alphanumerics | Critical |
| Stripe Publishable Key | `pk_live_` + 24+ alphanumerics | Medium |

### Communication

| Name | Pattern prefix | Severity |
|------|---------------|----------|
| Slack Bot Token | `xoxb-` + 11 digits + `-` + 11 digits + `-` + 24 alphanumerics | Critical |
| Slack App Token | `xapp-` + digit + `-` + 10 chars + `-` + 13 digits + `-` + 64 chars | Critical |
| Slack Webhook URL | `hooks.slack.com/services/T.../B.../` + 24 chars | High |
| Twilio Account SID | `AC` + 32 lowercase alphanumerics | High |
| Twilio Auth Token | `twilio` + quoted 32-char lowercase value | High |
| SendGrid API Key | `SG.` + 22 chars + `.` + 43 chars | Critical |

### Infrastructure

| Name | Pattern prefix | Severity |
|------|---------------|----------|
| npm Auth Token | `npm_` + 36 alphanumerics | High |
| Docker Hub PAT | `dockerhub` + quoted 32+ char value | High |
| Heroku API Key | `heroku` + quoted UUID-format value | High |
| PagerDuty Key | 32 lowercase alphanumerics | Medium |
| Datadog API Key | `datadog` + quoted 32 hex chars | High |
| Cloudflare API Key | 37 hex chars | Medium |
| Cloudflare API Token | `cloudflare` + quoted 40-char value | High |

### Cryptographic Material

| Name | Pattern | Severity |
|------|---------|----------|
| RSA Private Key | PEM header: `BEGIN RSA PRIVATE KEY` | Critical |
| EC Private Key | PEM header: `BEGIN EC PRIVATE KEY` | Critical |
| Generic Private Key | PEM header: `BEGIN PRIVATE KEY` | Critical |
| PGP Private Key | PEM header: `BEGIN PGP PRIVATE KEY BLOCK` | Critical |
| JWT Token | `eyJ` + base64 + `.` + base64 + `.` + base64 | High |

### Credentials and Connection Strings

| Name | Pattern | Severity |
|------|---------|----------|
| Generic API Key | `api_key` / `apikey` / `api_secret` + quoted 20+ char value | High |
| Generic Password | `password` / `passwd` / `pwd` + quoted 8+ char value | Medium |
| Generic Token | `token` / `secret` + quoted 20+ char value | High |
| Database Connection String | `postgresql://` / `mysql://` / `mongodb://` / `redis://` / `amqp://` + credentials + `@` | Critical |

---

## 5. Entropy Gating

Entropy gating prevents false positives where the matched string is clearly not a real credential (e.g., placeholder values like `your-api-key-here` or all-X strings).

**Shannon entropy calculation:**

```python
def _entropy(s: str) -> float:
    if not s:
        return 0.0
    freq = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    return -sum(
        (f / len(s)) * math.log2(f / len(s))
        for f in freq.values()
    )
```

**Entropy values for common string types:**

| String type | Entropy range | Classification |
|-------------|--------------|----------------|
| All same character (`aaaaaaa`) | 0.00 | Placeholder — skip |
| Common placeholder text (`your-api-key`) | 3.2–3.8 | Placeholder — skip |
| AWS documentation example key | ~4.0 | Known example — whitelisted |
| Random hex string (28+ chars) | 4.2–4.5 | Borderline |
| Real-format Stripe key | High entropy | Real credential |
| Cryptographically random token | 5.0+ | Almost certainly real |

**Threshold:** `HIGH_ENTROPY_THRESHOLD = 4.5`

Patterns marked `entropy_required=True` require entropy ≥ 4.5 on the extracted value. Highly-specific patterns (e.g., GitHub PAT exact format of prefix + 36 chars) are marked `entropy_required=False` — the format specificity makes any match likely real regardless of entropy.

---

## 6. False Positive Suppression

### 1. Whitelisted example strings

Known documentation strings that appear legitimately in repositories:

```python
def _fp(parts: list[str]) -> str:
    """
    Joins string parts at runtime.
    Source file contains function calls — not scannable literals.
    """
    return "".join(parts)

FALSE_POSITIVE_VALUES = {
    # AWS documentation examples (assembled at runtime — not literals)
    _fp(["AKIA", "IOSFODNN7EXAMPLE"]),
    _fp(["wJalrXUtnFEMI", "/K7MDENG/", "bPxRfiCYEXAMPLEKEY"]),
    # Token placeholders — all X's (assembled at runtime)
    _fp(["gh" + "p_", "X" * 36]),
    _fp(["sk" + "_live_", "X" * 24]),
    _fp(["xo" + "xb-", "XXXX-XXXX-XXXX"]),
    # Generic placeholders
    "your-api-key-here",
    "your_api_key",
    "placeholder",
    "changeme",
    "example",
    "test_key_not_real",
    "insert_key_here",
    "replace_with_real_key",
}
```

**Why `_fp()` for token-like strings?** GitHub Secret Scanning reads source file text. If `FALSE_POSITIVE_VALUES` contained a Stripe-format string as a literal (even one made of all X's), GitHub would flag this file. The `_fp()` helper assembles strings at runtime — the source contains only function calls, which scanners cannot match against credential patterns.

### 2. File type skipping

```python
FALSE_POSITIVE_FILE_PATTERNS = [
    r"\.md$",           # Markdown documentation
    r"\.txt$",          # Plain text
    r"\.example$",      # Example files
    r"\.sample$",       # Sample files
    r"test_",           # Test files (prefix)
    r"_test\.",         # Test files (suffix)
    r"/tests/",         # Test directories
    r"docs/",           # Documentation directories
    r"README",          # README files
    r"CHANGELOG",       # Changelog files
    r"CONTRIBUTING",    # Contributing guidelines
    r"\.env\.example",  # Example env files
    r"\.env\.sample",   # Sample env files
]
```

### 3. Test fixture line detection

```python
def _is_test_line(line: str) -> bool:
    """Skip lines that are clearly test fixtures or documentation examples."""
    line_lower = line.lower()

    # Comment markers indicating examples
    test_markers = ["# example", "# test", "# demo", "# sample"]
    if any(m in line_lower for m in test_markers):
        return True

    # Word-boundary markers — \b prevents matching inside compound tokens
    # e.g. avoids matching 'fake' inside 'p4ssw0rd_not_fake'
    word_markers = [r"\bmock\b", r"\bfake\b", r"\bdummy\b"]
    return any(re.search(m, line_lower) for m in word_markers)
```

**Why `\bfake\b` and not a simple substring `fake`?**

The substring `fake` appears inside legitimate variable names like `p4ssw0rd_not_fake@example.com` — a password format used in some test fixtures that still need scanning. Word-boundary regex `\bfake\b` matches `fake` only as a standalone word (surrounded by non-word characters), not as part of a larger compound token.

This bug was identified during test writing. The `_connection_string()` test helper returned a postgres URL containing `_not_fake` in the password segment. The original substring check `_fake` caused `_is_test_line()` to return `True`, silently skipping the connection string scan in tests.

### 4. Placeholder regex detection

```python
def _is_false_positive(value: str) -> bool:
    v_lower = value.lower()

    for fp in FALSE_POSITIVE_VALUES:
        if fp.lower() in v_lower or v_lower in fp.lower():
            return True

    # Common placeholder patterns
    if re.search(r"(x{6,}|placeholder|example|changeme|your[_-]|insert)", v_lower):
        return True

    return False
```

`x{6,}` catches strings with 6+ consecutive X's — the most common placeholder format in docs and README examples.

---

## 7. Alert Deduplication

Without deduplication, a leaked credential in a frequently-modified file generates a new GitHub issue on every push — creating alert fatigue and obscuring real new findings.

**Deduplication key — order-independent hash of pattern names:**

```python
def _findings_dedup_key(findings: list[SecretFinding]) -> str:
    pattern_names = sorted(f.pattern_name for f in findings)
    raw = "|".join(pattern_names)
    return hashlib.md5(raw.encode()).hexdigest()[:12]
```

Sorted before hashing so `["GitHub PAT", "AWS Key"]` and `["AWS Key", "GitHub PAT"]` produce the same key — they represent the same credential set regardless of order found.

**Redis dedup check:**

```python
def _already_reported(repo: str, dedup_key: str, ttl_seconds: int = 3600) -> bool:
    try:
        r = get_redis()
        redis_key = f"secret_reported:{repo}:{dedup_key}"
        result = r.set(redis_key, "1", nx=True, ex=ttl_seconds)
        return result is None   # None = key existed = already reported
    except Exception:
        return False   # Redis failure → allow alert (fail open for security)
```

**Why fail open on Redis failure?** For security alerts, a duplicate alert is annoying. A missed credential exposure is a security incident. The inverse of the idempotency choice (where we prefer missing events over duplicating them).

**TTL = 1 hour:** Short enough that a new push 2 hours later (developer forgot to rotate) generates a fresh alert. Long enough to absorb rapid-push workflows without spamming.

---

## 8. Severity Classification

| Severity | Meaning | Example types |
|----------|---------|--------------|
| `critical` | Immediate action required — real financial or security impact | AWS keys, GitHub tokens, payment keys, private keys, DB connection strings |
| `high` | Significant risk — rotate within hours | GCP keys, communication platform tokens, infrastructure tokens |
| `medium` | Moderate risk — review and rotate soon | CDN/monitoring keys, publishable keys, unclassified high-entropy strings |

Alert table ordering: critical → high → medium, ensuring the most dangerous exposures appear first.

---

## 9. Alert Format

The GitHub issue created by a detection:

```markdown
## Secret Detection Alert

**2 potential secret(s) detected:** CRITICAL: 1 | HIGH: 1

> Immediate action required: Rotate ALL exposed credentials NOW.
> Assume they are compromised — they may already be indexed by external scanners.

| Line | Type | Severity | Confidence | Redacted Match |
|------|------|----------|------------|----------------|
| 47 (app/config.py) | GitHub PAT (classic) | critical | High | [prefix]****..****[suffix] |
| 89 (app/config.py) | GCP API Key | high | High | [prefix]****..****[suffix] |

### How to fix
1. Rotate the exposed credential immediately (revoke + regenerate)
2. Remove from git history: `git filter-repo --path <file> --invert-paths`
3. Add to `.gitignore` and use environment variables instead
4. Audit access logs for unauthorized use of the exposed credential

Use a secrets manager (GitHub Secrets, Vault, AWS SSM) — never hardcode credentials.
```

**Redaction format:** Never logs actual credential values. Shows first 4 and last 4 characters separated by asterisks:

```python
def _redact(matched: str) -> str:
    if len(matched) <= 12:
        return "***"
    return matched[:4] + ("*" * min(len(matched) - 8, 20)) + matched[-4:]

# A 40-character token → first4*****(20 asterisks)*****last4
```

---

## 10. Source File Safety

The scanner source (`enhanced_secrets.py`) and its tests (`test_enhanced_secrets.py`) must not contain any string that looks like a real credential — otherwise GitHub Secret Scanning flags the scanner's own source.

**The rule:** No credential-format strings as literals anywhere in source or documentation. All credential-like strings must be assembled at runtime.

### In `enhanced_secrets.py` — the `_fp()` helper

```python
def _fp(parts: list[str]) -> str:
    """
    Assembles strings from parts at runtime.

    GitHub Secret Scanning operates on source file text — it reads the raw
    file looking for patterns like 'sk_live_' followed by alphanumerics.
    By storing strings as lists of fragments and joining at runtime, the
    source file never contains the assembled pattern as a literal.

    The scanner sees: _fp(["sk" + "_live_", "X" * 24])
    At runtime produces: the assembled Stripe placeholder string (all X's)
    Scanner never sees the assembled form in source text.
    """
    return "".join(parts)
```

### In tests — helper functions

```python
def _github_pat() -> str:
    """Valid-format GitHub classic token for tests. Not a real token."""
    # Split so no single fragment matches the full scanner pattern
    return "gh" + "p_" + "aBcDeFgHiJkLmNoPqRsTuVw" + "XyZ12345678"

def _stripe_live_key() -> str:
    """Valid-format Stripe live key for tests. Not a real key."""
    return "sk" + "_live_" + "AbCdEfGhIjKlMnOpQr" + "StUvWxYz1234"

def _slack_bot_token() -> str:
    """Valid-format Slack bot token for tests. Not a real token."""
    return "xo" + "xb-" + "12345678901" + "-" + "12345678901" + "-ABCDefGhIjKlMnOpQrStUvWx"
```

**Why split at the prefix boundary?** Credential scanners match on specific prefix patterns (`sk_live_`, `ghp_`, etc.) followed by high-entropy content. Splitting `"sk_live_"` into `"sk" + "_live_"` means no source line contains the triggering prefix as a single token.

**Important:** When `test_enhanced_secrets.py` was first pushed, GitHub Secret Scanning flagged it for containing a Stripe-format string literal used as a test input value. GitHub created a security alert, the alert required manual dismissal, and the file required a forced rewrite. This incident is why the `_fp()` pattern and helper function approach was established as mandatory for all credential-like strings.

---

## 11. Known Limitations

**Novel credential formats.** New services with new token formats are missed unless their pattern is added. The pattern list must be maintained as the ecosystem evolves.

**Low-entropy secrets.** Short passwords or dictionary-word keys (`password123`, `mysecretkey`) score below the entropy threshold and are not caught by the entropy-only detector. Real credentials from modern services (Stripe, GitHub, AWS) all use high-entropy formats by default.

**Indirect leakage.** A credential base64-encoded inside a Kubernetes Secret YAML is not decoded and scanned at the content level. The base64 string may trigger the entropy-only detector but the credential type cannot be identified.

**Committed-then-immediately-deleted.** If a developer commits a credential and immediately creates a second commit deleting it (before the bot processes the push), the deletion commit is not scanned (removed lines are skipped). The credential is still in git history and must be treated as compromised.

**GitHub API rate limits on large pushes.** The scanner fetches each commit's diff individually. A push with 50 commits on a busy repository may approach GitHub App rate limits. The scanner processes as many commits as possible and logs a warning for any skipped.

---

## 12. Comparison to GitHub Native Secret Scanning

| Feature | GitHub Native | This Scanner |
|---------|--------------|-------------|
| Infrastructure | GitHub's servers | Your Render service |
| Pattern count | 200+ (GitHub-managed) | 35+ (this codebase) |
| Pattern updates | Automatic | Manual addition to `PATTERNS` list |
| Alert location | Security tab → Secret Scanning | GitHub Issue (visible to all collaborators) |
| Custom patterns | GitHub Advanced Security (paid) | Free — add to `PATTERNS` list |
| Entropy detection | Yes | Yes — threshold 4.5 |
| False positive suppression | GitHub's algorithm | `_fp()` + whitelist + word-boundary regex |
| Alert deduplication | Built in | Redis `SET NX`, 1-hour TTL |
| Historical repo scan | Yes (entire history) | No — push-time only |
| PR scan | With Advanced Security | Yes — via `/security` command |
| Cost | Free for public repos / paid for private | Free (Render + Redis free tier) |

**Recommendation:** Use both systems together. GitHub's native scanner covers 200+ patterns updated by GitHub's security team. This scanner provides immediate push-time GitHub Issues with rotation instructions, PR-level scanning via `/security`, and custom pattern support without paying for GitHub Advanced Security.
