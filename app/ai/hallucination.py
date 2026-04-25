"""
app/ai/hallucination.py
V4 Sprint 3: Hallucination detector for LLM responses.

Problem: LLMs sometimes make up:
  - File names that don't exist in the repo
  - Function names that aren't in the code
  - CVE IDs that don't exist
  - Commit SHAs that aren't real
  - Confident claims about things they can't know

This module catches the most common cases before they
reach the user as GitHub comments.

Usage:
    from app.ai.hallucination import check_response

    result = check_response(
        response={"fix": "def auth()...", "root_cause": "..."},
        context={"files": ["app/auth.py"], "repo": "org/repo"},
        response_type="fix"
    )
    if result.confidence < 0.5:
        log.warning(f"Low confidence response: {result.warnings}")
"""

import re
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Patterns that indicate hallucination
_HALLUCINATION_PATTERNS = [
    # LLM uncertainty phrases — should not appear in confident responses
    (r"\bi'm not sure\b", "uncertainty", 0.3),
    (r"\bi don't know\b", "uncertainty", 0.3),
    (r"\bas an ai\b", "ai_disclosure", 0.2),
    (r"\bi cannot access\b", "access_claim", 0.4),
    (r"\bi don't have access\b", "access_claim", 0.4),
    (r"\bI apologize\b", "apology", 0.1),
    # Fabricated CVE patterns (real CVEs: GHSA-xxxx-xxxx-xxxx or CVE-YYYY-NNNNN)
    (r"\bCVE-\d{4}-\d{4,}\b", "cve_reference", 0.0),  # valid — no penalty
    (r"\bGHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}\b", "ghsa_reference", 0.0),
    # Placeholder text that should have been filled
    (r"\[insert [^\]]+\]", "placeholder", 0.5),
    (r"\[your [^\]]+\]", "placeholder", 0.5),
    (r"\bTODO\b", "todo_in_response", 0.2),
    (r"\bXXX\b", "xxx_placeholder", 0.3),
    # Overly confident claims about runtime behavior
    (r"\bthis will definitely\b", "overconfidence", 0.1),
    (r"\bguaranteed to\b", "overconfidence", 0.1),
    (r"\balways works\b", "overconfidence", 0.1),
]

# Minimum field lengths — too short = likely hallucinated
_MIN_LENGTHS = {
    "fix": 20,
    "root_cause": 10,
    "explanation": 15,
    "summary": 10,
    "description": 20,
}


@dataclass
class HallucinationResult:
    confidence: float  # 0.0 = definitely hallucinated, 1.0 = looks clean
    warnings: list[str] = field(default_factory=list)
    is_acceptable: bool = True
    penalized_fields: list[str] = field(default_factory=list)

    @property
    def should_block(self) -> bool:
        """True if response is too suspicious to show user."""
        return self.confidence < 0.3 or not self.is_acceptable


def check_response(
    response: dict,
    context: dict | None = None,
    response_type: str = "generic",
) -> HallucinationResult:
    """
    Validate an LLM response for hallucination signals.

    Args:
        response:      Parsed JSON dict from LLM
        context:       Optional context dict with repo files, commits etc
        response_type: "fix" | "pr_analysis" | "code_review" | "generic"

    Returns:
        HallucinationResult with confidence score and warnings
    """
    warnings = []
    penalty = 0.0
    penalized_fields = []
    context = context or {}

    # 1. Check for empty/error response
    if not response or response.get("error"):
        return HallucinationResult(
            confidence=0.1,
            warnings=["Response is empty or contains error"],
            is_acceptable=False,
        )

    if response.get("raw"):
        return HallucinationResult(
            confidence=0.2,
            warnings=["Response could not be parsed as JSON — raw text returned"],
            is_acceptable=False,
        )

    # 2. Check all string fields for hallucination patterns
    text_content = _extract_text(response)

    for pattern, label, pen in _HALLUCINATION_PATTERNS:
        if re.search(pattern, text_content, re.IGNORECASE):
            if pen > 0:
                warnings.append(f"Hallucination signal: {label}")
                penalty += pen
                penalized_fields.append(label)

    # 3. Check for suspiciously short required fields
    for field_name, min_len in _MIN_LENGTHS.items():
        val = response.get(field_name, "")
        if val and isinstance(val, str) and len(val.strip()) < min_len:
            warnings.append(
                f"Field '{field_name}' suspiciously short ({len(val.strip())} chars)"
            )
            penalty += 0.15
            penalized_fields.append(field_name)

    # 4. File reference validation (if repo files provided)
    repo_files = set(context.get("files", []))
    if repo_files:
        referenced = _extract_file_refs(text_content)
        for ref in referenced:
            if ref not in repo_files and not _is_plausible_file(ref, repo_files):
                warnings.append(f"References file not in PR: {ref}")
                penalty += 0.1
                penalized_fields.append(f"file_ref:{ref}")

    # 5. SHA validation (if commits provided)
    known_shas = set(context.get("commits", []))
    if known_shas:
        referenced_shas = re.findall(r"\b[0-9a-f]{7,40}\b", text_content)
        for sha in referenced_shas:
            if sha not in known_shas and not any(s.startswith(sha) for s in known_shas):
                warnings.append(f"References unknown commit SHA: {sha[:7]}")
                penalty += 0.05

    # 6. Score calculation
    confidence = max(0.0, min(1.0, 1.0 - penalty))

    result = HallucinationResult(
        confidence=round(confidence, 2),
        warnings=warnings,
        penalized_fields=penalized_fields,
        is_acceptable=confidence >= 0.3,
    )

    if warnings:
        log.info(
            f"hallucination.check type={response_type} "
            f"confidence={result.confidence} warnings={len(warnings)}"
        )

    return result


def add_confidence_footer(comment: str, result: HallucinationResult) -> str:
    """
    Optionally append a confidence note to GitHub comment.
    Only shown when confidence is below 0.7.
    """
    if result.confidence >= 0.7:
        return comment

    if result.confidence >= 0.5:
        note = f"\n\n> 🤔 **AI Confidence: {int(result.confidence * 100)}%** — Please verify before applying."
    else:
        note = f"\n\n> ⚠️ **Low confidence ({int(result.confidence * 100)}%)** — This response may need manual review."

    return comment + note


def _extract_text(response: dict) -> str:
    """Extract all string values from response dict for pattern checking."""
    parts = []
    for val in response.values():
        if isinstance(val, str):
            parts.append(val)
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.extend(v for v in item.values() if isinstance(v, str))
    return " ".join(parts)


def _extract_file_refs(text: str) -> list[str]:
    """Extract likely file references from text."""
    # Match things like app/auth.py, src/utils.ts, tests/test_foo.py
    return re.findall(r"\b[\w/]+\.\w{2,4}\b", text)


def _is_plausible_file(ref: str, known_files: set[str]) -> bool:
    """
    Returns True if the file reference is plausibly related to known files.
    e.g. "auth.py" plausibly refers to "app/auth.py"
    """
    basename = ref.split("/")[-1]
    return any(f.endswith(basename) for f in known_files)
