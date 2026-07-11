"""
app/github/patch_parser.py — V6.2
Pure helpers for mapping AI review findings onto GitHub diff positions.

GitHub's Pulls Review API only accepts inline comments on lines that appear
in the diff (added or context lines of the NEW file version). The AI returns
approximate line references ("~42", "42-45", "around line 40"); these helpers
parse the unified-diff patch GitHub already gives us per file, enumerate the
line numbers a comment may legally anchor to, and snap an approximate target
to the nearest legal line.

No network, no state — deliberately unit-test friendly.
"""

from __future__ import annotations

import re

# New-file line numbers a PR review comment may anchor to, mapped to
# (line_content, is_added). Context lines are commentable but a committable
# ``suggestion`` block only makes sense on an added line.
CommentableLines = dict[int, tuple[str, bool]]

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def commentable_lines(patch: str) -> CommentableLines:
    """
    Parse a unified-diff `patch` (as returned by GitHub's files API) into
    {new_file_line_number: (content, is_added)} for every line that exists in
    the new file version (added '+' and context ' ' lines; '-' lines belong
    to the old version and are skipped).
    """
    lines: CommentableLines = {}
    if not patch:
        return lines
    new_ln = 0
    in_hunk = False
    for raw in patch.splitlines():
        m = _HUNK_RE.match(raw)
        if m:
            new_ln = int(m.group(1))
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if raw.startswith("+"):
            lines[new_ln] = (raw[1:], True)
            new_ln += 1
        elif raw.startswith("-"):
            continue  # old-version line — not commentable, no new-line advance
        elif raw.startswith("\\"):
            continue  # "\ No newline at end of file"
        else:
            # context line (starts with ' ' or is empty)
            lines[new_ln] = (raw[1:] if raw.startswith(" ") else raw, False)
            new_ln += 1
    return lines


def parse_line_ref(ref) -> int | None:
    """
    Extract the first line number from an AI line reference.
    Accepts ints or strings like "42", "~42", "42-45", "around line 40".
    Returns None when no number is present ("?", "", None).
    """
    if isinstance(ref, int):
        return ref if ref > 0 else None
    if not ref:
        return None
    m = re.search(r"\d+", str(ref))
    return int(m.group()) if m else None


def nearest_commentable(
    target: int | None, lines: CommentableLines, max_distance: int = 5
) -> int | None:
    """
    Snap `target` to the nearest legally-commentable line within
    `max_distance`. Prefers added lines over context lines on a distance tie
    (the finding is almost certainly about the new code). Returns None when
    nothing is close enough — the caller should keep that finding in the
    summary body instead of guessing.
    """
    if target is None or not lines:
        return None
    best: int | None = None
    best_key: tuple[int, int] | None = None
    for ln, (_content, is_added) in lines.items():
        dist = abs(ln - target)
        if dist > max_distance:
            continue
        key = (dist, 0 if is_added else 1)
        if best_key is None or key < best_key:
            best, best_key = ln, key
    return best


def make_suggestion_block(fix: str, anchor_line: int, lines: CommentableLines) -> str:
    """
    Return a committable ```suggestion block for `fix`, or "" when a
    suggestion would be unsafe. GitHub applies a suggestion by REPLACING the
    anchored line, so we only emit one when:
      - the fix is a single line of code (no newlines, sane length),
      - the anchor is an ADDED line (suggesting over unchanged context is
        usually wrong), and
      - the fix actually differs from the current line content.
    Anything else belongs in a normal fenced code block.
    """
    if not fix or "\n" in fix or len(fix) > 200:
        return ""
    entry = lines.get(anchor_line)
    if entry is None:
        return ""
    content, is_added = entry
    if not is_added or fix.strip() == content.strip():
        return ""
    # Preserve the original line's indentation when the model dropped it.
    if not fix[:1].isspace() and content[: len(content) - len(content.lstrip())]:
        indent = content[: len(content) - len(content.lstrip())]
        if not fix.startswith(indent):
            fix = indent + fix.lstrip()
    return f"```suggestion\n{fix}\n```"
