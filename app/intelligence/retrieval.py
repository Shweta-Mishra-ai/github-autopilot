"""
Context Retrieval - app/intelligence/retrieval.py
V3: Retrieve relevant code context for AI using vector similarity.
Used to give AI better context when reviewing PRs or answering questions.
"""

from app.core.logger import get_logger

log = get_logger(__name__)

DEFAULT_TOP_K = 5
MAX_CONTEXT_CHARS = 4000


def get_relevant_context(
    repo: str,
    query: str,
    top_k: int = DEFAULT_TOP_K,
    exclude_files: list[str] = None
) -> str:
    """
    Retrieve most relevant code chunks for a given query.
    Returns formatted context string ready to inject into AI prompt.
    """
    try:
        from app.intelligence.embeddings import _get_model, _get_collection

        model = _get_model()
        collection = _get_collection(repo)

        # Check if collection has any documents
        count = collection.count()
        if count == 0:
            log.debug("retrieval.empty_collection", repo=repo)
            return ""

        query_embedding = model.encode(query).tolist()

        where = None
        if exclude_files:
            # Exclude specific files (e.g. the file being changed)
            where = {"filepath": {"$nin": exclude_files}}

        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=min(top_k, count),
            where=where,
            include=["documents", "metadatas", "distances"]
        )

        if not results or not results["documents"]:
            return ""

        docs = results["documents"][0]
        metas = results["metadatas"][0]
        distances = results["distances"][0]

        # Build context string
        context_parts = []
        total_chars = 0

        for doc, meta, dist in zip(docs, metas, distances):
            # Skip low relevance results (high distance = low similarity)
            if dist > 0.8:
                continue

            filepath = meta.get("filepath", "unknown")
            snippet = doc[:800]

            part = f"### {filepath}\n```\n{snippet}\n```\n"

            if total_chars + len(part) > MAX_CONTEXT_CHARS:
                break

            context_parts.append(part)
            total_chars += len(part)

        if not context_parts:
            return ""

        context = "\n".join(context_parts)
        log.info("retrieval.context_built",
                 repo=repo, chunks=len(context_parts), chars=total_chars)

        return f"## Relevant Codebase Context\n\n{context}"

    except Exception as e:
        log.error("retrieval.failed", repo=repo, error=str(e))
        return ""


def get_context_for_pr(repo: str, changed_files: list[dict]) -> str:
    """
    Build context for PR review by finding related code.
    changed_files: list of {filename, patch} dicts from GitHub API.
    """
    if not changed_files:
        return ""

    # Build query from changed file names and patches
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
        exclude_files=changed_paths  # Don't include the files being changed
    )


def get_context_for_issue(repo: str, title: str, body: str) -> str:
    """Build context for issue triage by finding related code."""
    query = f"{title}\n{body[:300]}"
    return get_relevant_context(repo=repo, query=query, top_k=3)

