# Prompt Injection Defense

## Overview

GitHub Autopilot processes untrusted user input from GitHub issues and PRs.
This makes it a target for **prompt injection attacks** (OWASP LLM Top 10: LLM01).

## Threat Model

An attacker can post malicious content in an issue/PR that manipulates the LLM into:
1. Ignoring system instructions
2. Outputting sensitive information
3. Generating malicious code via `/autofix`
4. Manipulating confidence scores to bypass guardrails

## Defense Architecture

Our defense follows a **defense-in-depth** strategy:
┌─────────────────────────────────────────────────────────────────┐
│  LAYER 1: INPUT VALIDATION                                      │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  • Unicode NFKC normalization                           │    │
│  │  • Zero-width character stripping                       │    │
│  │  • 15+ injection pattern signatures                     │    │
│  └─────────────────────────────────────────────────────────┘    │
└──────────────────────────────────┬──────────────────────────────┘
│
▼
┌─────────────────────────────────────────────────────────────────┐
│  LAYER 2: STRUCTURAL SEPARATION                                 │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  • User content wrapped in non-guessable delimiters     │    │
│  │  • System prompt wrapped in separate delimiters           │    │
│  │  • Explicit security rule in system prompt              │    │
│  └─────────────────────────────────────────────────────────┘    │
└──────────────────────────────────┬──────────────────────────────┘
│
▼
┌─────────────────────────────────────────────────────────────────┐
│  LAYER 3: FAIL-CLOSED POLICY                                    │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  🔴 Critical → Reject input entirely                    │    │
│  │  🟡 High     → Truncate at first finding                │    │
│  │  🟢 Medium   → Log and continue with caution            │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘

## Data Flow
GitHub Issue/PR Comment
│
▼
┌─────────────────┐
│  Raw User Input │  ← Untrusted
└────────┬────────┘
│
▼
┌─────────────────────────┐
│  _normalize_for_scan()  │  ← NFKC + strip zero-width + collapse
└────────┬────────────────┘
│
▼
┌─────────────────────────┐
│ _detect_injection()     │  ← 15+ regex patterns
└────────┬────────────────┘
│
┌─────┴─────┐
▼           ▼
Critical     Safe/Benign
│              │
▼              ▼
Reject      ┌─────────────┐
(empty)     │ _sanitize() │
└──────┬──────┘
│
▼
┌──────────────┐
│  Wrap in     │
│  delimiters  │
└──────┬───────┘
│
▼
┌──────────────┐
│  Send to LLM │
│  (secured)   │
└──────────────┘

## Bypass Techniques We Defend Against

| Technique | Example | Defense |
|-----------|---------|---------|
| Direct override | "Ignore previous instructions" | Pattern matching on normalized text |
| Leet speak | "1gn0re pr3v10us" | NFKC normalization |
| Newline separation | "ignore\nprevious\ninstructions" | Whitespace collapse |
| Zero-width chars | "i\u200bgn\u200bore" | Unicode category filtering |
| Unicode homoglyphs | Cyrillic 'і' instead of Latin 'i' | NFKC normalization |
| Delimiter breakout | "```json\n{\"role\":\"system\"}" | Nested delimiter detection |

## Testing

Run the security test suite:

```bash
pytest tests/test_router_security.py -v
References
OWASP LLM Top 10
OWASP LLM AI Security & Privacy Guide
