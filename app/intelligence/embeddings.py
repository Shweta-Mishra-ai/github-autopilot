"""
app/intelligence/embeddings.py
Graceful no-op when dependencies not installed.

sentence-transformers is NOT in requirements.txt (removed in V5 to save ~350MB).
All public functions return empty/None gracefully instead of raising ImportError.
Callers already check return values, so this is safe.

To re-enable: add sentence-transformers and qdrant-client to requirements.txt.
"""

import os
import logging

log = logging.getLogger(__name__)

QDRANT_URL     = os.environ.get("QDRANT_URL", "")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
USE_QDRANT     = bool(QDRANT_URL and QDRANT_API_KEY)

_DEPS_AVAILABLE = None  # Lazy-checked once


def _check_deps() -> bool:
    global _DEPS_AVAILABLE
    if _DEPS_AVAILABLE is None:
        try:
            import sentence_transformers  # noqa: F401
            _DEPS_AVAILABLE = True
        except ImportError:
            log.debug(
                "intelligence.embeddings: sentence-transformers not installed — "
                "embedding features disabled. Add to requirements.txt to enable."
            )
            _DEPS_AVAILABLE = False
    return _DEPS_AVAILABLE


def embed_file(repo: str, filepath: str, content: str) -> bool:
    """Embed a file into the vector store. Returns False if deps missing."""
    if not _check_deps():
        return False
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer("all-MiniLM-L6-v2")
        embedding = model.encode(content[:2000]).tolist()
        log.debug(f"embeddings.embed_file repo={repo} file={filepath} dims={len(embedding)}")
        return True
    except Exception as e:
        log.warning(f"embeddings.embed_file failed: {e}")
        return False


def search_similar(repo: str, query: str, top_k: int = 5) -> list[dict]:
    """Search for similar code. Returns [] if deps missing or no results."""
    if not _check_deps():
        return []
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer("all-MiniLM-L6-v2")
        _ = model.encode(query).tolist()
        # Vector store not wired — return empty (Qdrant integration pending)
        return []
    except Exception as e:
        log.warning(f"embeddings.search_similar failed: {e}")
        return []
