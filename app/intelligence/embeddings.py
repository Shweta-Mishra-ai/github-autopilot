"""
Embeddings - app/intelligence/embeddings.py
V3: Generate and cache code embeddings using sentence-transformers.
Embeddings are stored in ChromaDB for fast retrieval.
"""

import os
import hashlib
from typing import List
from app.core.logger import get_logger

log = get_logger(__name__)

MODEL_NAME = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
CHROMA_DIR = os.environ.get("CHROMA_DIR", "data/chroma")

_model = None
_client = None
_collection = None


def _get_model():
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer(MODEL_NAME)
            log.info("embeddings.model_loaded", model=MODEL_NAME)
        except Exception as e:
            log.error("embeddings.model_load_failed", error=str(e))
            raise
    return _model


def _get_collection(repo: str):
    global _client
    try:
        import chromadb
        if _client is None:
            _client = chromadb.PersistentClient(path=CHROMA_DIR)

        # Sanitize repo name for collection name
        collection_name = repo.replace("/", "_").replace("-", "_")[:63]
        return _client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"}
        )
    except Exception as e:
        log.error("embeddings.collection_failed", error=str(e))
        raise


def embed_file(repo: str, filepath: str, content: str, commit_sha: str = "") -> bool:
    """
    Embed a single file and store in ChromaDB.
    Uses file path + commit SHA as unique ID.
    """
    try:
        model = _get_model()
        collection = _get_collection(repo)

        # Split into chunks if file is large
        chunks = _chunk_code(content, filepath)

        for i, chunk in enumerate(chunks):
            doc_id = hashlib.sha256(
                f"{repo}:{filepath}:{commit_sha}:{i}".encode()
            ).hexdigest()[:32]

            embedding = model.encode(chunk).tolist()

            collection.upsert(
                ids=[doc_id],
                embeddings=[embedding],
                documents=[chunk],
                metadatas=[{
                    "repo": repo,
                    "filepath": filepath,
                    "commit_sha": commit_sha,
                    "chunk_index": i,
                }]
            )

        log.info("embeddings.file_indexed",
                 repo=repo, filepath=filepath, chunks=len(chunks))
        return True

    except Exception as e:
        log.error("embeddings.embed_failed",
                  repo=repo, filepath=filepath, error=str(e))
        return False


def embed_files_batch(repo: str, files: list[dict], commit_sha: str = "") -> int:
    """
    Embed multiple files. Each dict should have 'path' and 'content'.
    Returns number of successfully embedded files.
    """
    success = 0
    for f in files:
        if embed_file(repo, f["path"], f["content"], commit_sha):
            success += 1
    log.info("embeddings.batch_complete", repo=repo, success=success, total=len(files))
    return success


def _chunk_code(content: str, filepath: str, max_chars: int = 1500) -> list[str]:
    """
    Split code into chunks for embedding.
    Tries to split at function/class boundaries.
    """
    if len(content) <= max_chars:
        return [f"File: {filepath}\n\n{content}"]

    lines = content.splitlines()
    chunks = []
    current_chunk = [f"File: {filepath}"]
    current_len = len(filepath)

    for line in lines:
        # Start new chunk at function/class definitions if chunk is getting large
        if (current_len > max_chars * 0.7 and
                (line.startswith("def ") or
                 line.startswith("class ") or
                 line.startswith("async def "))):
            if current_chunk:
                chunks.append("\n".join(current_chunk))
            current_chunk = [f"File: {filepath}", line]
            current_len = len(filepath) + len(line)
        else:
            current_chunk.append(line)
            current_len += len(line)

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks or [content[:max_chars]]

