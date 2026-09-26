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

import ast
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


# A `fix` that is an instruction rather than a replacement line. The review
# prompt asks for "exact fix" and models answer "Add a null check before
# dereferencing user" about as often as they answer with code. Emitted as a
# ``suggestion that is a one-click Commit button which replaces working code
# with an English sentence — the most damaging thing this bot can render,
# because GitHub presents it as a reviewed, ready-to-apply patch.
#
# Guessed at with a word list first, which let "Should be >= not >" and
# "Compare with is None rather than ==" through on the `=` inside them. So ask
# a parser instead of guessing: a replacement line has to BE code, and code
# parses. Each wrapper below supplies the context a bare fragment is missing —
# a decorator needs a def under it, `await` needs an async def around it, a
# block header needs a body — so the fragment is judged on its own syntax
# rather than on where it happens to sit.
_UNWRAP_C_FAMILY = re.compile(r"^\s*(?:const|let|var)\s+")
_TRY_CLAUSE = re.compile(r"^\s*(?:except|finally)\b")
_IF_CLAUSE = re.compile(r"^\s*(?:elif|else)\b")

# A model declining to answer. `n/a` is the trap: it parses cleanly as one name
# divided by another, so the parser calls it code and a reviewer gets a button
# that replaces a working line with `n/a`. "None" is deliberately absent — that
# is a real fix — so the comparison is case-sensitive on the lowered form only.
_NON_ANSWER = {
    "n/a",
    "na",
    "n.a.",
    "n/a.",
    "tbd",
    "todo",
    "-",
    "--",
    "?",
    "??",
    "???",
    "unknown",
    "unclear",
    "see above",
    "as above",
    "no fix",
    "no change",
}

_WRAPPERS = (
    lambda s: s,
    lambda s: "async def _():\n    " + s,  # await, return, yield, break
    lambda s: s + "\n    pass",  # def/if/for/while/with header
    lambda s: s + "\ndef _(): pass",  # @decorator
    lambda s: "_f(" + s + ")",  # keyword argument
    # `foo.bar();` and `const x = 1;` — the common JS/TS single-line forms.
    lambda s: _UNWRAP_C_FAMILY.sub("", s).rstrip(";"),
    # A dangling clause, which needs its opening statement above it.
    lambda s: ("try:\n    pass\n" + s + "\n    pass") if _TRY_CLAUSE.match(s) else s,
    lambda s: ("if _x:\n    pass\n" + s + "\n    pass") if _IF_CLAUSE.match(s) else s,
)


def _looks_like_code(fix: str) -> bool:
    """
    True when `fix` parses as code rather than reading as a sentence about code.

    Judged with Python's own parser, which also accepts the C-family lines this
    bot sees most (`foo.bar();`, `const x = 1;`). A typed declaration in a
    language we cannot parse — `int x = 1;` — is a known false negative, and
    that is the side to be wrong on: a false negative renders the fix in a
    plain fenced block, which is merely less convenient, while a false positive
    ships a committable suggestion that breaks the branch.
    """
    s = fix.strip()
    if not s or s.lower() in _NON_ANSWER:
        return False
    for wrap in _WRAPPERS:
        try:
            ast.parse(wrap(s))
            return True
        except (SyntaxError, ValueError, MemoryError, RecursionError):
            continue
    return False


def make_suggestion_block(fix: str, anchor_line: int, lines: CommentableLines) -> str:
    """
    Return a committable ```suggestion block for `fix`, or "" when a
    suggestion would be unsafe. GitHub applies a suggestion by REPLACING the
    anchored line, so we only emit one when:
      - the fix is a single line of code (no newlines, sane length),
      - the fix reads as code rather than as an instruction about code,
      - the anchor is an ADDED line (suggesting over unchanged context is
        usually wrong), and
      - the fix actually differs from the current line content.
    Anything else belongs in a normal fenced code block.
    """
    if not fix or "\n" in fix or len(fix) > 200:
        return ""
    if not _looks_like_code(fix):
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
