"""
app/intelligence/ — what the bot learns and derives about a repository:
memory (memory.py), thread summaries (summarizer.py) and the code map (codegraph.py).

There is no embedding or vector-search layer. One existed as a stub — it
encoded text and discarded the result, and its search always returned
nothing — while the docs advertised Qdrant. It was removed (2026-10).
"""
