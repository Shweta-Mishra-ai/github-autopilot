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
Layer 1: Input Validation
Unicode normalization (NFKC)
Zero-width character stripping
Pattern detection with 15+ injection signatures
Layer 2: Structural Separation
User content wrapped in non-guessable delimiters
System prompt wrapped in separate delimiters
Explicit security rule in system prompt
Layer 3: Fail-Closed Policy
Critical injections → reject input entirely
High injections → truncate at first finding
All attempts logged with severity

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
