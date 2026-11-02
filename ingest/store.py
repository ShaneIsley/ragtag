"""The Azure AI Search index, behind a small interface tests can fake."""

import json
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import SearchIndex

from app.settings import Settings, require
from ingest.chunk import Chunk

INDEX_FILE = Path(__file__).resolve().parents[1] / "search" / "index.json"
UPLOAD_BATCH = 500


class ChunkStore(Protocol):
    def ensure_index(self) -> bool:
        """Create the index if it's missing. True if it was created."""
        ...

    def existing_ids(self, source_id: str) -> set[str]: ...

    def source_ids(self) -> set[str]:
        """Every source with passages in the index."""
        ...

    def upload(self, documents: Sequence[dict[str, Any]]) -> None: ...

    def delete(self, ids: Iterable[str]) -> None: ...


class AzureSearchStore:
    def __init__(self, settings: Settings) -> None:
        require(settings, "search_endpoint", "search_admin_key", "search_index")
        credential = AzureKeyCredential(settings.search_admin_key.get_secret_value())
        self._index_name = settings.search_index
        self._indexes = SearchIndexClient(settings.search_endpoint, credential)
        self._search = SearchClient(settings.search_endpoint, self._index_name, credential)

    def ensure_index(self) -> bool:
        try:
            self._indexes.get_index(self._index_name)
            return False
        except ResourceNotFoundError:
            definition = json.loads(INDEX_FILE.read_text())
            definition["name"] = self._index_name
            self._indexes.create_index(SearchIndex(definition))
            return True

    def existing_ids(self, source_id: str) -> set[str]:
        results = self._search.search(
            search_text="*",
            filter=f"source_id eq '{source_id}'",
            select=["id"],
            top=100_000,
        )
        return {r["id"] for r in results}

    def source_ids(self) -> set[str]:
        results = self._search.search(search_text="*", facets=["source_id,count:1000"], top=0)
        return {f["value"] for f in (results.get_facets() or {}).get("source_id", [])}

    def upload(self, documents: Sequence[dict[str, Any]]) -> None:
        for start in range(0, len(documents), UPLOAD_BATCH):
            results = self._search.merge_or_upload_documents(
                list(documents[start : start + UPLOAD_BATCH])
            )
            failed = [r.key for r in results if not r.succeeded]
            if failed:
                raise RuntimeError(f"{len(failed)} documents failed to upload, e.g. {failed[0]}")

    def delete(self, ids: Iterable[str]) -> None:
        documents = [{"id": i} for i in ids]
        for start in range(0, len(documents), UPLOAD_BATCH):
            self._search.delete_documents(documents[start : start + UPLOAD_BATCH])


def to_document(chunk: Chunk, vector: list[float], now: datetime | None = None) -> dict[str, Any]:
    """A chunk as an index document. Field names match search/index.json."""
    s = chunk.source
    return {
        "id": chunk.id,
        "source_id": s.id,
        "collection": s.collection,
        "title": s.title,
        "section": chunk.section,
        "content": chunk.content,
        "url": s.url,
        "publisher": s.publisher,
        "licence": s.licence,
        "classification": s.classification,
        "content_hash": chunk.content_hash,
        "updated_at": (now or datetime.now(UTC)).isoformat(),
        "content_vector": vector,
    }
