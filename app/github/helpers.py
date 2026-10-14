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


# GitHub serves at most 3,000 files for one pull request, 100 per page.
PR_FILES_PER_PAGE = 100
PR_FILES_MAX_PAGES = 30


def pr_files(repo: str, number: int, token: str, get=None) -> list:
    """
    Every file in a pull request — not the first 30.

    Five call sites fetched `/pulls/{n}/files` once, and GitHub's default page
    is 30 files. On any larger PR the rest were never reviewed, never checked
    for test gaps, never scanned for secrets and never counted: this
    repository's own PR #108, at 32 files, was reported as "Files: 30 ·
    +2101 −235" when it was +3,618 −249.

    Any page failing raises. A later-page failure used to keep the pages
    already fetched and only log, and no caller could tell: the PR report said
    "Files: 100" for a 250-file PR, the risk recorded for the head (which
    /merge trusts) was judged on 100 of them, and /security said "No secrets
    detected in the 100 changed file(s) scanned" with the secret in file 180.
    A partial list presented as the whole PR is worse than none. `get`
    defaults to the GitHub client and exists so a caller can pass the name its
    own tests patch.
    """
    if get is None:
        from app.github.client import gh_get as get

    files: list = []
    for page in range(1, PR_FILES_MAX_PAGES + 1):
        path = f"/repos/{repo}/pulls/{number}/files?per_page={PR_FILES_PER_PAGE}&page={page}"
        batch = get(path, token)
        if not isinstance(batch, list) or not batch:
            break
        files.extend(batch)
        if len(batch) < PR_FILES_PER_PAGE:
            break
    else:
        log.warning(
            f"pr_files.capped repo={repo} pr={number} files={len(files)} — GitHub lists at "
            "most 3,000 files for a pull request"
        )
    return files
