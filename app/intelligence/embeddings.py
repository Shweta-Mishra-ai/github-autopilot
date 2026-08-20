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
import threading

log = logging.getLogger(__name__)

QDRANT_URL = os.environ.get("QDRANT_URL", "")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
USE_QDRANT = bool(QDRANT_URL and QDRANT_API_KEY)

MODEL_NAME = "all-MiniLM-L6-v2"

_DEPS_AVAILABLE = None  # Lazy-checked once

# The encoder is loaded once and reused. Both public functions previously
# constructed SentenceTransformer(...) on every call, which re-reads ~90MB of
# weights from disk per file embedded — on a push touching 30 files that is 30
# model loads inside the handler thread. Guarded by a lock because the dispatch
# thread pool calls these concurrently.
_model = None
_model_lock = threading.Lock()


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


def _get_model():
    """Return the shared encoder, loading it on first use. None if unavailable."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:  # re-check: another thread may have loaded it
                from sentence_transformers import SentenceTransformer

                log.info(f"embeddings.model_loading name={MODEL_NAME}")
                _model = SentenceTransformer(MODEL_NAME)
    return _model


def reset_model_cache() -> None:
    """Drop the cached encoder. Test hook; also usable to reclaim memory."""
    global _model
    with _model_lock:
        _model = None


def embed_file(repo: str, filepath: str, content: str) -> bool:
    """Embed a file into the vector store. Returns False if deps missing."""
    if not _check_deps():
        return False
    try:
        embedding = _get_model().encode(content[:2000]).tolist()
        log.debug(f"embeddings.embed_file repo={repo} file={filepath} dims={len(embedding)}")
        return True
    except Exception as e:
        log.warning(f"embeddings.embed_file failed: {e}")
        return False


def search_similar(repo: str, query: str, top_k: int = 5) -> list[dict]:
    """
    Search for similar code. Returns [] if deps missing or no results.

    NOTE: the vector store is not wired up yet, so this returns [] even when
    the encoder is available. Every caller treats [] as "no context", so the
    retrieval layer degrades to no-context rather than failing.
    """
    if not _check_deps():
        return []
    try:
        _ = _get_model().encode(query).tolist()
        # Vector store not wired — return empty (Qdrant integration pending)
        return []
    except Exception as e:
        log.warning(f"embeddings.search_similar failed: {e}")
        return []
