"""
app/intelligence/ — Optional embedding-based retrieval layer.

STATUS: Not active in production.

sentence-transformers and qdrant-client are NOT in requirements.txt.
All functions in this package gracefully return empty results when
dependencies are missing. No ImportError will propagate to callers.

To activate:
  1. pip install sentence-transformers qdrant-client (or add to requirements.txt)
  2. Set QDRANT_URL + QDRANT_API_KEY in environment
  3. Remove the _INTELLIGENCE_ACTIVE guard below
"""

_INTELLIGENCE_ACTIVE = False
