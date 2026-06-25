"""
app/core/sanitizer.py
Structured prompt injection defense.

"""

import re
import unicodedata
import logging
import contextlib

log = logging.getLogger(__name__)

# ── Injection patterns ────────────────────────────────────────────────────────
# Each tuple: (compiled_pattern, replacement_label)
# Applied in order — stops at first match per pattern (replaces, continues scan).
_INJECTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"ignore\s+(all\s+)?previous\s+instructions?", re.I), "INSTR_INJ"),
    (re.compile(r"forget\s+(all\s+)?previous", re.I), "INSTR_INJ"),
    (re.compile(r"disregard\s+(all\s+)?previous", re.I), "INSTR_INJ"),
    (re.compile(r"you\s+are\s+now\b", re.I), "ROLE_INJ"),
    (re.compile(r"act\s+as\s+(a|an|the)\b", re.I), "ROLE_INJ"),
    (re.compile(r"pretend\s+(you\s+are|to\s+be)\b", re.I), "ROLE_INJ"),
    (re.compile(r"your\s+new\s+(role|persona|identity)\s+is", re.I), "ROLE_INJ"),
    (re.compile(r"jailbreak", re.I), "JAILBREAK"),
    (re.compile(r"DAN\s+mode", re.I), "JAILBREAK"),
    (re.compile(r"<\s*system\s*>", re.I), "XML_INJ"),
    (re.compile(r"<\s*/?\s*instructions?\s*>", re.I), "XML_INJ"),
    (re.compile(r"\[INST\]", re.I), "DELIM_INJ"),
    (re.compile(r"<<SYS>>", re.I), "DELIM_INJ"),
    (re.compile(r"###\s*System", re.I), "DELIM_INJ"),
    (re.compile(r"###\s*Human", re.I), "DELIM_INJ"),
    (re.compile(r"reveal\s+(your\s+)?(system\s+)?prompt", re.I), "EXFIL"),
    (re.compile(r"show\s+me\s+your\s+(instructions?|prompt)", re.I), "EXFIL"),
    (re.compile(r"print\s+your\s+(system|initial)\s+prompt", re.I), "EXFIL"),
]


def sanitize_user_input(text: str, max_chars: int = 8_000) -> str:
    """
    Sanitize text from user-controlled sources (GitHub webhook payloads).
    Applies Unicode normalization + injection pattern replacement.

    Does NOT raise. Returns sanitized string.
    """
    if not text:
        return ""

    # Hard cap
    text = text[:max_chars]

    # Unicode normalization — collapse lookalike characters
    with contextlib.suppress(Exception):
        text = unicodedata.normalize("NFKC", text)

    # Pattern replacement
    hits = []
    for pattern, label in _INJECTION_PATTERNS:
        new_text, n = pattern.subn(f"[{label}]", text)
        if n:
            hits.append(label)
            text = new_text

    if hits:
        log.warning(f"sanitizer.injection_detected patterns={hits}")

    return text


def wrap_user_content(text: str, label: str = "USER_INPUT") -> str:
    """
    Wrap user-controlled content in explicit XML-style delimiters.

    Use this when inserting webhook payload text (commit messages, issue bodies,
    PR titles) into LLM prompts so the model can unambiguously distinguish
    user content from system instructions.

    Example:
        prompt = f"Review this commit message:\n{wrap_user_content(commit_msg)}"

    The delimiters are intentionally verbose so they survive prompt truncation.
    """
    return f"<{label}>\n{text}\n</{label}>"
