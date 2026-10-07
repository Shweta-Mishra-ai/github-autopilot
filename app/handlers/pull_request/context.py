"""
app/handlers/pull_request/context.py
The unchanged code around a change, for the reviewer to read.

The review used to see the diff and nothing else. A hunk shows a few lines of
context at most, so the model reported what it could not see as missing — "no
error handling" for a try that opens ten lines above the hunk, "undefined
variable" for a parameter of the enclosing function. Codebase retrieval was
meant to supply this and has never returned anything (the vector store was
never wired up).

This fetches each reviewed file at the PR's head commit — one Contents API
call, no index, no model — and returns the whole enclosing function for each
changed line: by the AST for Python, by a line window for anything else.
"""

from __future__ import annotations

import ast
import base64
import logging

from app.github.patch_parser import commentable_lines

log = logging.getLogger(__name__)

# A file bigger than this is not worth fetching for context: decoding and
# parsing it costs more than the few functions the review can show from it.
MAX_FILE_BYTES = 300_000
# Lines either side of a change, when no enclosing function is known.
WINDOW = 15
# An enclosing function longer than this is shown only around its changes.
MAX_FUNCTION_LINES = 120


def _changed_lines(patch: str) -> list[int]:
    return sorted(n for n, (_c, added) in commentable_lines(patch).items() if added)


def _python_ranges(source: str, changed: list[int]) -> list[tuple[int, int]] | None:
    """The innermost function or class enclosing each changed line, or None
    when the source does not parse (the caller falls back to windows)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    scopes = [
        (n.lineno, n.end_lineno or n.lineno)
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    ranges = []
    for ln in changed:
        enclosing = [s for s in scopes if s[0] <= ln <= s[1]]
        if not enclosing:
            ranges.append((ln - WINDOW, ln + WINDOW))
            continue
        start, end = min(enclosing, key=lambda s: s[1] - s[0])  # innermost
        if end - start > MAX_FUNCTION_LINES:
            start, end = max(start, ln - WINDOW * 2), min(end, ln + WINDOW * 2)
        ranges.append((start, end))
    return ranges


def _merge(ranges: list[tuple[int, int]], last: int) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted((max(1, s), min(last, e)) for s, e in ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]  # noqa: C416 — lists to tuples


def surrounding_code(source: str, filename: str, patch: str, budget: int) -> str:
    """
    Numbered lines of `source` enclosing the changes in `patch`, at most
    `budget` characters, cut on a line boundary. "" when there is nothing to
    add or no room for it.
    """
    changed = _changed_lines(patch)
    lines = source.splitlines()
    if not changed or not lines or budget <= 0:
        return ""

    ranges = _python_ranges(source, changed) if filename.endswith(".py") else None
    if ranges is None:
        ranges = [(ln - WINDOW, ln + WINDOW) for ln in changed]

    out: list[str] = []
    size = 0
    for start, end in _merge(ranges, len(lines)):
        block = [f"{n:>6}   {lines[n - 1]}" for n in range(start, end + 1)]
        if out:
            block.insert(0, "   ...")
        for line in block:
            if size + len(line) + 1 > budget:
                return "\n".join(out)
            out.append(line)
            size += len(line) + 1
    return "\n".join(out)


def fetch_file(repo: str, path: str, ref: str, token: str, get) -> str:
    """
    The file at `ref`, decoded, or "" when it cannot or should not be read
    (deleted, binary, too large, or any API error). Never raises: context is
    an aid to the review, and its absence must not cost the review itself.
    """
    try:
        data = get(f"/repos/{repo}/contents/{path}?ref={ref}", token)
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            return ""
        if int(data.get("size") or 0) > MAX_FILE_BYTES:
            return ""
        return base64.b64decode(data.get("content") or "").decode("utf-8")
    except Exception as e:
        log.debug(f"review_context.fetch_failed path={path}: {e}")
        return ""
