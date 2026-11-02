"""Hybrid keyword and vector search over the policy index."""

from collections import Counter
from dataclasses import dataclass
from typing import Any, Protocol

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.models import QueryDebugMode, VectorizedQuery

from app.embedding import Embedder
from app.settings import Settings, require

TOP = 12
# Search returns this many, and at most PER_SOURCE of them from any one source go on,
# so one long page can't fill every slot (31 Oct 2026: four IG Policy clauses crowded
# out the approval process; a cap of 3 lost answers that need several passages).
CANDIDATES = 30
PER_SOURCE = 4
VECTOR_K = 50
SEARCH_FIELDS = ["title", "section", "content"]
SELECT = ["id", "source_id", "title", "section", "url", "publisher", "content"]


@dataclass(frozen=True)
class Passage:
    id: str
    source_id: str
    title: str
    section: str
    url: str
    publisher: str
    content: str
    # Cosine similarity of the passage to the question itself (not its rewrite), or None
    # if the question's vector search didn't find it. The hybrid score is a fused rank,
    # so the decline rule uses this.
    similarity: float | None


class Retriever(Protocol):
    def search(self, question: str, rewrite: str | None = None) -> list[Passage]: ...


class AzureRetriever:
    def __init__(self, settings: Settings, embedder: Embedder) -> None:
        require(settings, "search_endpoint", "search_query_key", "search_index")
        self._client = SearchClient(
            settings.search_endpoint,
            settings.search_index,
            AzureKeyCredential(settings.search_query_key.get_secret_value()),
        )
        self._embedder = embedder

    def search(self, question: str, rewrite: str | None = None) -> list[Passage]:
        """One hybrid request for the question and, if given, its broader rewrite: both
        texts for keyword matching and one vector query each, merged by rank fusion."""
        queries = [question] if not rewrite or rewrite == question else [question, rewrite]
        vectors = self._embedder.embed(queries)
        results = self._client.search(
            search_text=" ".join(queries),
            search_fields=SEARCH_FIELDS,
            # Match any word of the question, not every word: the prototype's lesson.
            search_mode="any",
            vector_queries=[
                VectorizedQuery(vector=v, k_nearest_neighbors=VECTOR_K, fields="content_vector")
                for v in vectors
            ],
            select=SELECT,
            top=CANDIDATES,
            # Returns each result's vector similarity alongside the fused score.
            debug=QueryDebugMode.VECTOR,
        )
        return diversify([to_passage(r) for r in results])


def diversify(
    passages: list[Passage], top: int = TOP, per_source: int = PER_SOURCE
) -> list[Passage]:
    """The first `top` passages in rank order, with at most `per_source` from any source."""
    kept: list[Passage] = []
    counts: Counter[str] = Counter()
    for p in passages:
        if counts[p.source_id] < per_source:
            kept.append(p)
            counts[p.source_id] += 1
            if len(kept) == top:
                break
    return kept


def to_passage(result: dict[str, Any]) -> Passage:
    return Passage(
        id=result["id"],
        source_id=result["source_id"],
        title=result["title"],
        section=result.get("section") or "",
        url=result["url"],
        publisher=result.get("publisher") or "",
        content=result["content"],
        similarity=vector_similarity(result),
    )


def vector_similarity(result: dict[str, Any]) -> float | None:
    """The similarity from the first vector query, which is the question's."""
    info = result.get("@search.document_debug_info") or {}
    subscores = (info.get("vectors") or {}).get("subscores") or {}
    first = next(iter(subscores.get("vectors") or []), None) or {}
    score = first.get("content_vector")
    return float(score["vectorSimilarity"]) if score else None
