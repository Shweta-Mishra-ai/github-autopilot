"""
app/github/helpers.py — Shared GitHub API helpers.
"""

import logging

log = logging.getLogger(__name__)


def fmt_error(label: str, e: Exception, limit: int = 200) -> str:
    """Standard error comment format used by all slash commands."""
    return f"## ⚠️ {label}\n\n`{str(e)[:limit]}`"


def repo_file_context(repo: str, token: str, ref: str = "HEAD", extra=(), get=None) -> dict:
    """
    `{"files": [...]}` naming every file in the repository at `ref`, plus
    `extra`, for the hallucination checker — or `{}` when that list cannot be
    had in full.

    The checker penalises a response for naming a file not in `context["files"]`.
    Handing it only the PR's changed files makes that check wrong by design for
    any analysis of a change's reach: /impact and /arch exist to name the files a
    change affects, which are mostly files it did not touch. Against the whole
    tree, a flagged file is one that does not exist — which is the thing worth
    catching.

    Fails open. A truncated tree (GitHub stops at 100,000 entries) cannot prove a
    file is absent, and neither can an error, so both return `{}` and the check
    is skipped rather than guessed at. `extra` carries files the PR adds, which
    are not on `ref` yet. `get` defaults to the GitHub client and exists so a
    caller can pass the name its own tests patch.
    """
    if get is None:
        from app.github.client import gh_get as get

    try:
        tree = get(f"/repos/{repo}/git/trees/{ref}?recursive=1", token)
    except Exception as e:
        log.debug(f"repo_file_context.unavailable repo={repo} ref={ref}: {e}")
        return {}

    if not isinstance(tree, dict) or tree.get("truncated"):
        return {}
    entries = tree.get("tree")
    paths = {
        t["path"]
        for t in (entries if isinstance(entries, list) else [])
        if isinstance(t, dict) and t.get("type") == "blob" and t.get("path")
    }
    if not paths:
        return {}
    return {"files": sorted(paths | {e for e in extra if e})}
