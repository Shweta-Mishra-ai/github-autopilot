"""
app/core/redaction.py — scrub text before it enters long-lived memory.

Repo memory used to be gated behind an opt-in env var precisely because it
could hold source code and credentials, which meant it was inert in every
standard cloud deployment: the brain neither learned nor recalled anything.

Redacting at the boundary is the better trade. What memory keeps is prose,
file paths and symbol names — the things that actually make recall useful —
while code bodies and anything secret-shaped are dropped before storage.
"""

from __future__ import annotations

import re

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INDENTED_BLOCK_RE = re.compile(r"(?m)^(?: {4}|\t).*$")

_CODE_PLACEHOLDER = "[code omitted]"
_SECRET_PLACEHOLDER = "[REDACTED]"

# Leading unmasked run of a redacted match, e.g. "ghp_" from
# "ghp_********************aaaa". Anything shorter than 4 chars is too generic
# to substitute on without risking collateral damage to ordinary prose.
_PREFIX_RE = re.compile(r"[^*.]+")


def redact(text: str | None) -> str:
    """
    Remove secrets and code bodies. Returns prose safe to persist.

    Best-effort and never raises: memory is an enhancement, and a redaction
    failure must not take down the command that triggered the write. The
    structural strip (fences, indented blocks) runs first and unconditionally,
    so even if the secret scan fails the bulk of any code body is already gone.
    """
    if not text:
        return ""

    text = _FENCE_RE.sub(_CODE_PLACEHOLDER, text)
    text = _INDENTED_BLOCK_RE.sub(_CODE_PLACEHOLDER, text)

    try:
        from app.security.enhanced_secrets import scan_diff

        # scan_diff only inspects lines beginning with "+", so present each
        # line as a diff addition.
        as_diff = "\n".join(f"+{line}" for line in text.splitlines())
        for finding in scan_diff(as_diff):
            # redacted_match looks like "ghp_********************aaaa" — the
            # leading run before the mask is the only part we can match on.
            prefix = _PREFIX_RE.match(finding.redacted_match or "")
            token = prefix.group(0) if prefix else ""
            if len(token) >= 4:
                text = re.sub(re.escape(token) + r"\S*", _SECRET_PLACEHOLDER, text)
    except Exception:
        pass  # structural strip above already ran

    return text
