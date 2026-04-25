"""
Embeddings - app/intelligence/embeddings.py
V3: Generate and cache code embeddings.
Primary: Qdrant Cloud (QDRANT_URL + QDRANT_API_KEY env vars)
Fallback: Local ChromaDB
If neither available: gracefully skip
"""

import os
import hashlib
import logging

log = logging.getLogger(__name__)

QDRANT_URL = os.environ.get("QDRANT_URL", "")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
CHROMA_DIR = os.environ.get("CHROMA_DIR", "data/chroma")

USE_QDRANT = bool(QDRANT_URL and QDRANT_API_KEY)
COLLECTION_NAME = "code_embeddings"
EMBEDDING_DIM = 384

_model = None
_qdrant_client = None
_chroma_client = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer("all-MiniLM-L6-v2")
        log.info("Embedding model loaded")
    return _model


def _get_qdrant():
    global _qdrant_client
    if _qdrant_client is None:
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams

        _qdrant_client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
        existing = [c.name for c in _qdrant_client.get_collections().collections]
        if COLLECTION_NAME not in existing:
            _qdrant_client.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=VectorParams(
                    size=EMBEDDING_DIM, distance=Distance.COSINE
                ),
            )
    return _qdrant_client


def _get_chroma(repo: str):
    global _chroma_client
    import chromadb

    if _chroma_client is None:
        os.makedirs(CHROMA_DIR, exist_ok=True)
        _chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)
    name = repo.replace("/", "_").replace("-", "_")[:63]
    return _chroma_client.get_or_create_collection(
        name=name, metadata={"hnsw:space": "cosine"}
    )


def embed_file(repo: str, filepath: str, content: str, commit_sha: str = "") -> bool:
    try:
        model = _get_model()
        chunks = _chunk_code(content, filepath)
        for i, chunk in enumerate(chunks):
            doc_id = hashlib.sha256(
                f"{repo}:{filepath}:{commit_sha}:{i}".encode()
            ).hexdigest()[:32]
            embedding = model.encode(chunk).tolist()
            if USE_QDRANT:
                _store_qdrant(doc_id, embedding, chunk, repo, filepath, commit_sha, i)
            else:
                _store_chroma(repo, doc_id, embedding, chunk, filepath, commit_sha, i)
        backend = "Qdrant" if USE_QDRANT else "ChromaDB"
        log.info(f"Indexed {filepath} ({len(chunks)} chunks) [{backend}]")
        return True
    except Exception as e:
        log.error(f"Embed failed: {filepath} — {e}")
        return False


def _store_qdrant(doc_id, embedding, chunk, repo, filepath, commit_sha, idx):
    from qdrant_client.models import PointStruct

    client = _get_qdrant()
    point_id = int(doc_id[:16], 16)
    client.upsert(
        collection_name=COLLECTION_NAME,
        points=[
            PointStruct(
                id=point_id,
                vector=embedding,
                payload={
                    "repo": repo,
                    "filepath": filepath,
                    "commit_sha": commit_sha,
                    "content": chunk[:500],
                },
            )
        ],
    )


def _store_chroma(repo, doc_id, embedding, chunk, filepath, commit_sha, idx):
    collection = _get_chroma(repo)
    collection.upsert(
        ids=[doc_id],
        embeddings=[embedding],
        documents=[chunk],
        metadatas=[{"repo": repo, "filepath": filepath, "commit_sha": commit_sha}],
    )


def search_similar(repo: str, query: str, top_k: int = 5) -> list:
    try:
        model = _get_model()
        query_embedding = model.encode(query).tolist()
        if USE_QDRANT:
            return _search_qdrant(query_embedding, repo, top_k)
        else:
            return _search_chroma(repo, query_embedding, top_k)
    except Exception as e:
        log.error(f"Search failed: {e}")
        return []


def _search_qdrant(query_embedding, repo, top_k):
    client = _get_qdrant()
    results = client.search(
        collection_name=COLLECTION_NAME,
        query_vector=query_embedding,
        query_filter={"must": [{"key": "repo", "match": {"value": repo}}]},
        limit=top_k,
    )
    return [
        {
            "filepath": r.payload.get("filepath", ""),
            "content": r.payload.get("content", ""),
            "score": r.score,
        }
        for r in results
        if r.score > 0.3
    ]


def _search_chroma(repo, query_embedding, top_k):
    try:
        collection = _get_chroma(repo)
        count = collection.count()
        if count == 0:
            return []
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=min(top_k, count),
            include=["documents", "metadatas", "distances"],
        )
        return [
            {"filepath": m.get("filepath", ""), "content": d[:500], "score": 1 - dist}
            for d, m, dist in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
            )
            if (1 - dist) > 0.3
        ]
    except Exception:
        return []


def embed_files_batch(repo: str, files: list, commit_sha: str = "") -> int:
    success = 0
    for f in files:
        if embed_file(repo, f["path"], f["content"], commit_sha):
            success += 1
    return success


def _chunk_code(content: str, filepath: str, max_chars: int = 1500) -> list:
    if len(content) <= max_chars:
        return [f"File: {filepath}\n\n{content}"]
    lines = content.splitlines()
    chunks, current, current_len = [], [f"File: {filepath}"], len(filepath)
    for line in lines:
        if current_len > max_chars * 0.7 and any(
            line.startswith(p) for p in ("def ", "class ", "async def ")
        ):
            chunks.append("\n".join(current))
            current = [f"File: {filepath}", line]
            current_len = len(filepath) + len(line)
        else:
            current.append(line)
            current_len += len(line)
    if current:
        chunks.append("\n".join(current))
    return chunks or [content[:max_chars]]
