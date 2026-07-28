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


# Zero-width and invisible formatting characters. Interleaving these lets
# "ignore previous instructions" survive any literal or whitespace-tolerant
# pattern, because the characters render as nothing but break the match.
_ZERO_WIDTH_RE = re.compile("[​‌‍⁠﻿­]")

_WHITESPACE_RE = re.compile(r"\s+")

# Labels indicating a deliberate override or exfiltration attempt rather than
# an unlucky turn of phrase. Masking these still feeds the surrounding
# attacker-authored text to the model, so the input is rejected instead.
_CRITICAL_LABELS = frozenset({"EXFIL", "DELIM_INJ", "XML_INJ"})


class InjectionRejected(Exception):
    """Raised when input contains a critical-severity injection attempt."""


def sanitize_user_input(text: str, max_chars: int = 8_000, fail_closed: bool = True) -> str:
    """
    Sanitize text from user-controlled sources (GitHub webhook payloads).

    Defence in depth, in order:
      1. Hard length cap
      2. NFKC normalisation      — collapses homoglyphs (Cyrillic е → Latin e)
      3. Zero-width stripping    — removes ​-style separators
      4. Whitespace collapse     — so "ignore\\n previous\\n instructions"
                                   cannot slip past a single-space pattern
      5. Pattern replacement
      6. Fail-closed on critical severity

    Raises InjectionRejected for a critical hit when fail_closed is True.
    Otherwise does not raise.
    """
    if not text:
        return ""

    text = text[:max_chars]

    with contextlib.suppress(Exception):
        text = unicodedata.normalize("NFKC", text)

    # The probe replaces zero-width characters with a SPACE, while the returned
    # text simply drops them. That matters: an attacker can use a zero-width
    # character *instead of* a space ("ignore<ZWSP>previous<ZWSP>instructions"),
    # so deleting it would fuse the words and defeat the pattern. Substituting
    # a space in the matching copy catches both that and the interleaved form.
    probe = _WHITESPACE_RE.sub(" ", _ZERO_WIDTH_RE.sub(" ", text)).strip()
    text = _ZERO_WIDTH_RE.sub("", text)

    hits = []
    for pattern, label in _INJECTION_PATTERNS:
        if not pattern.search(probe):
            continue
        hits.append(label)

        if fail_closed and label in _CRITICAL_LABELS:
            log.warning(f"sanitizer.injection_rejected label={label}")
            raise InjectionRejected(f"Input rejected: {label}")

        text = pattern.sub(f"[{label}]", text)
        probe = pattern.sub(f"[{label}]", probe)

    if hits:
        log.warning(f"sanitizer.injection_detected patterns={hits}")

    # A pattern split across whitespace is masked in the probe but not in the
    # original. When that happens the original still carries the payload, so
    # return the collapsed form instead of leaking it.
    if hits and any(f"[{h}]" not in text for h in hits):
        return probe

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
