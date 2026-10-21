"""
app/handlers/pull_request/grounding.py
Is a review finding about a line that actually exists in the diff?

The review prompt asks for each finding's `line` AND the exact text of that
line (`code`). The text is checked here against the diff itself, which turns
the model's least reliable output — a line number — into something verified:

  - the quoted text is on the cited line          → grounded on that line
  - it is on a different changed line             → grounded, re-anchored there
  - it is only on unchanged context lines         → context, no anchor (body)
  - it is on a removed line                       → removed, no anchor (body)
  - it is nowhere in this file's diff             → misquoted
  - there is no quote, and the line is not exact  → unquoted

A quote may run over several consecutive lines, but all of it must be diff
text. It used to be enough for the quote to CONTAIN one real line, so
`return None  # then cursor.execute(f"... {req.args['id']}")` was grounded on
a real `return None` and the invented SQL injection published at critical.

A model can invent a bug; it cannot make a quote of code that is not in the
diff match the diff. A MISQUOTED finding is therefore evidence of invention
and is withheld (listed separately, never silently dropped). An UNQUOTED one
is only unproven — the model gave a wrong number and no text — so it is still
shown, marked as unverified, just never pinned to a line it may not be about.

Pure: no I/O, no state.
"""

from __future__ import annotations

import re

from app.github.patch_parser import commentable_lines, diff_lines, parse_line_ref

GROUNDED = "line"
REMOVED = "removed"
UNQUOTED = "unquoted"
MISQUOTED = "misquoted"
# Real code, but none of it changed in this PR. The prompt asks for findings
# on changed lines only; these were anchored as inline comments on code the
# author did not touch. Shown in the body, labelled, never pinned to a line.
CONTEXT = "context"

# A quote may span at most this many consecutive lines.
_WINDOW = 5

# A quote shorter than this ("}", "pass", "else:") matches too many lines to
# prove anything on its own, so it only counts on the exact cited line.
_MIN_QUOTE = 6

# To MOVE a finding off the line the model cited, the quote must identify the
# new line: at least this long, or at least half of that line.
_DISTINCTIVE_QUOTE = 25

# The review shows numbered lines ("    42 + code"); a model sometimes copies
# the number and marker along with the code.
_NUMBER_PREFIX = re.compile(r"^\s*\d*\s*[+\- ]\s")
_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    """Whitespace-collapsed text, without a copied "  42 + " line prefix."""
    return _WS.sub(" ", _NUMBER_PREFIX.sub("", text or "", count=1).strip())


def _removed_lines(patch: str) -> list[str]:
    # Every "-" line. GitHub's `patch` field carries no `---` file header, so
    # skipping "---" lines skipped real removed lines — a SQL `-- comment`, a
    # YAML or Markdown `---` — and a finding about one was withheld as invented.
    return [raw[1:] for raw in diff_lines(patch or "") if raw.startswith("-")]


def _spans(quote: str, seq: list[tuple[str, object]]) -> list[list[object]]:
    """
    Every run of consecutive lines whose joined text contains the WHOLE quote,
    as the list of keys it covers. `seq` is [(content, key)] in diff order.
    """
    norms = [(_norm(c), k) for c, k in seq]
    norms = [(t, k) for t, k in norms if t]
    found = []
    for i in range(len(norms)):
        joined, ends = "", []
        for t, _k in norms[i : i + _WINDOW]:
            joined = f"{joined} {t}" if joined else t
            ends.append(len(joined))
        at = joined.find(quote)
        # Must begin inside line i, so each span is reported once.
        if at < 0 or at >= ends[0]:
            continue
        stop = at + len(quote)
        covered = [k for _t, k in norms[i : i + _WINDOW]]
        found.append(covered[: next(n for n, e in enumerate(ends) if e >= stop) + 1])
    return found


def ground_finding(issue: dict, patch: str) -> tuple[str, int | None]:
    """
    (GROUNDED, line) | (CONTEXT, None) | (REMOVED, None) | (UNQUOTED, None)
    | (MISQUOTED, None).

    A finding with no `code` at all is judged on its line number alone: it is
    grounded only when the cited line is an ADDED line, never snapped to a
    neighbour — otherwise it is UNQUOTED.
    """
    lines = commentable_lines(patch)
    target = parse_line_ref(issue.get("line"))
    quote = _norm(str(issue.get("code") or ""))

    if not quote:
        # A number alone proves nothing about unchanged code: only a cited
        # ADDED line is taken at its word.
        if target is not None and target in lines and lines[target][1]:
            return GROUNDED, target
        return UNQUOTED, None

    if len(quote) < _MIN_QUOTE:
        if target in lines and _norm(lines[target][0]) == quote:
            return (GROUNDED, target) if lines[target][1] else (CONTEXT, None)
        # Too short to search for; not evidence of invention either.
        return UNQUOTED, None

    spans = _spans(quote, [(content, n) for n, (content, _added) in sorted(lines.items())])
    if spans:
        # Anchor on a changed line the quote covers, nearest the cited one.
        added = [n for span in spans for n in span if lines[n][1]]
        if not added:
            return CONTEXT, None
        if target in added:
            return GROUNDED, target
        # Moving a finding to a different line needs a quote that identifies
        # that line. "return" (6 characters) matched any return statement, so
        # an invented finding cited at line 40 was re-anchored to whichever
        # `return` was nearest — and given a commit button there.
        firsts = sorted({span[0] for span in spans})
        line_text = _norm(lines[firsts[0]][0]) if firsts else ""
        distinctive = len(quote) >= _DISTINCTIVE_QUOTE or len(quote) * 2 >= len(line_text)
        if len(firsts) > 1 or not distinctive:
            return UNQUOTED, None
        return GROUNDED, min(added, key=lambda n: (abs(n - target) if target else 0, n))

    if _spans(quote, [(r, i) for i, r in enumerate(_removed_lines(patch))]):
        return REMOVED, None
    return MISQUOTED, None
