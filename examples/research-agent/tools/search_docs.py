"""search_docs — stubbed retrieval tool for the research template.

Replace `_DOCS` with your real retriever (vector DB, BM25, etc.).
"""

from __future__ import annotations

from eap_core.mcp import mcp_tool

_DOCS: list[dict] = [
    {"id": "doc-1", "text": "Paris is the capital of France."},
    {"id": "doc-2", "text": "The Eiffel Tower is in Paris."},
    {"id": "doc-3", "text": "Lyon is the third-largest city in France."},
]


@mcp_tool(description="Search the local document store.")
async def search_docs(query: str, k: int = 3) -> list[dict]:
    """Stubbed substring search; replace with real retrieval."""
    q = query.lower()
    hits = [d for d in _DOCS if any(w in d["text"].lower() for w in q.split())]
    return hits[:k] if hits else _DOCS[:k]
