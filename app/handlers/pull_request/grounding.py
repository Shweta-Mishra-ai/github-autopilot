"""
app/handlers/pull_request/grounding.py
Is a review finding about a line that actually exists in the diff?

The review prompt asks for each finding's `line` AND the exact text of that
line (`code`). The text is checked here against the diff itself, which turns
the model's least reliable output — a line number — into something verified:

  - the quoted text is on the cited line          → grounded on that line
  - it is on a different changed line             → grounded, re-anchored there
  - it is on a removed line                       → grounded, no anchor (body)
  - it is nowhere in this file's diff             → misquoted
  - there is no quote, and the line is not exact  → unquoted

A model can invent a bug; it cannot make a quote of code that is not in the
diff match the diff. A MISQUOTED finding is therefore evidence of invention
and is withheld (listed separately, never silently dropped). An UNQUOTED one
is only unproven — the model gave a wrong number and no text — so it is still
shown, marked as unverified, just never pinned to a line it may not be about.

Pure: no I/O, no state.
"""

from __future__ import annotations

import re

from app.github.patch_parser import commentable_lines, parse_line_ref

GROUNDED = "line"
REMOVED = "removed"
UNQUOTED = "unquoted"
MISQUOTED = "misquoted"

# A quote shorter than this ("}", "pass", "else:") matches too many lines to
# prove anything on its own, so it only counts on the exact cited line.
_MIN_QUOTE = 6

# The review shows numbered lines ("    42 + code"); a model sometimes copies
# the number and marker along with the code.
_NUMBER_PREFIX = re.compile(r"^\s*\d*\s*[+\- ]\s")
_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    """Whitespace-collapsed text, without a copied "  42 + " line prefix."""
    return _WS.sub(" ", _NUMBER_PREFIX.sub("", text or "", count=1).strip())


def _removed_lines(patch: str) -> list[str]:
    return [
        raw[1:]
        for raw in (patch or "").splitlines()
        if raw.startswith("-") and not raw.startswith("---")
    ]


def _matches(quote: str, content: str) -> bool:
    line = _norm(content)
    if not line:
        return False
    if quote in line:
        return True
    # A quote spanning a line and a little more (two lines joined) still
    # proves the line, provided the line itself is not trivially short.
    return len(line) >= _MIN_QUOTE and line in quote


def ground_finding(issue: dict, patch: str) -> tuple[str, int | None]:
    """
    (GROUNDED, line) | (REMOVED, None) | (UNQUOTED, None) | (MISQUOTED, None).

    A finding with no `code` at all is judged on its line number alone: it is
    grounded only when the cited line is itself a changed or context line of
    the diff, never snapped to a neighbour — otherwise it is UNQUOTED.
    """
    lines = commentable_lines(patch)
    target = parse_line_ref(issue.get("line"))
    quote = _norm(str(issue.get("code") or ""))

    if not quote:
        if target is not None and target in lines:
            return GROUNDED, target
        return UNQUOTED, None

    if len(quote) < _MIN_QUOTE:
        if target in lines and _norm(lines[target][0]) == quote:
            return GROUNDED, target
        # Too short to search for; not evidence of invention either.
        return UNQUOTED, None

    hits = [n for n, (content, _added) in lines.items() if _matches(quote, content)]
    if hits:
        if target in hits:
            return GROUNDED, target
        # The quote is real but the number was wrong — the common case. Take
        # the matching line nearest to where the model pointed.
        return GROUNDED, min(hits, key=lambda n: (abs(n - target) if target else 0, n))

    if any(_matches(quote, removed) for removed in _removed_lines(patch)):
        return REMOVED, None
    return MISQUOTED, None
