"""
Context Retrieval - app/intelligence/retrieval.py
V4: Retrieve relevant code context for AI using vector similarity.

FIXED: _get_collection does not exist in embeddings.py.
       Now uses search_similar() public function directly.
       Gracefully returns "" when embeddings not available (Render free tier).
"""

import logging

log = logging.getLogger(__name__)

DEFAULT_TOP_K = 5
MAX_CONTEXT_CHARS = 4000


def get_relevant_context(
    repo: str,
    query: str,
    top_k: int = DEFAULT_TOP_K,
    exclude_files: list[str] = None,
) -> str:
    """
    Retrieve most relevant code chunks for a given query.
    Returns formatted context string ready to inject into AI prompt.
    Returns "" silently when vector DB not available (acceptable on free tier).
    """
    try:
        from app.intelligence.embeddings import search_similar

        results = search_similar(repo, query, top_k=top_k)

        if not results:
            return ""

        context_parts = []
        total_chars = 0
        exclude_set = set(exclude_files or [])

        for item in results:
            filepath = item.get("filepath", "unknown")
            if filepath in exclude_set:
                continue

            score = item.get("score", 1.0)
            # Skip low relevance (lower score = less similar in some DBs)
            if score < 0.2:
                continue

            content = item.get("content", item.get("text", ""))[:800]
            part = f"### {filepath}\n```\n{content}\n```\n"

            if total_chars + len(part) > MAX_CONTEXT_CHARS:
                break

            context_parts.append(part)
            total_chars += len(part)

        if not context_parts:
            return ""

        context = "\n".join(context_parts)
        log.info(
            f"retrieval.context_built repo={repo} "
            f"chunks={len(context_parts)} chars={total_chars}"
        )
        return f"## Relevant Codebase Context\n\n{context}"

    except Exception as e:
        log.debug(f"retrieval.failed repo={repo} error={e}")
        return ""  # Silent failure — context is optional enhancement


def get_context_for_pr(repo: str, changed_files: list[dict]) -> str:
    """
    Build context for PR review by finding related code.
    changed_files: list of {filename, patch} dicts from GitHub API.
    """
    if not changed_files:
        return ""

    query_parts = []
    changed_paths = []

    for f in changed_files[:5]:
        filepath = f.get("filename", "")
        patch = f.get("patch", "")[:200]
        changed_paths.append(filepath)
        if filepath:
            query_parts.append(filepath)
        if patch:
            query_parts.append(patch)

    query = "\n".join(query_parts)[:500]
    return get_relevant_context(
        repo=repo,
        query=query,
        top_k=4,
        exclude_files=changed_paths,
    )


def get_context_for_issue(repo: str, title: str, body: str) -> str:
    """Build context for issue triage by finding related code."""
    query = f"{title}\n{body[:300]}"
    return get_relevant_context(repo=repo, query=query, top_k=3)
