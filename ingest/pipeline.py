"""Fetch, convert, chunk, embed and index each source, skipping passages that haven't changed."""

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import httpx

from app.embedding import Embedder
from app.masking import mask_document
from ingest.chunk import Chunk, chunk_markdown
from ingest.convert import to_markdown
from ingest.fetch import fetch
from ingest.sources import Source
from ingest.store import ChunkStore, to_document

log = logging.getLogger("ingest")


@dataclass(frozen=True)
class Plan:
    """What a run changes for one source. Chunk IDs come from content hashes."""

    new: list[Chunk]
    unchanged: int
    stale: set[str]


def plan(chunks: list[Chunk], existing_ids: set[str]) -> Plan:
    current = {c.id for c in chunks}
    return Plan(
        new=[c for c in chunks if c.id not in existing_ids],
        unchanged=len(current & existing_ids),
        stale=existing_ids - current,
    )


@dataclass
class SourceResult:
    source_id: str
    status: str = "ok"
    chunks: int = 0
    added: int = 0
    unchanged: int = 0
    removed: int = 0
    error: str = ""
    # The sections of the passages that are new or changed, for a reviewer to read.
    sections_added: list[str] = field(default_factory=list)


@dataclass
class RunResult:
    index_created: bool = False
    results: list[SourceResult] = field(default_factory=list)
    skipped_unverified: list[str] = field(default_factory=list)
    # Passages removed per source that is no longer in sources.yaml.
    pruned: dict[str, int] = field(default_factory=dict)

    @property
    def failed(self) -> list[SourceResult]:
        return [r for r in self.results if r.status == "failed"]


def ingest_source(
    source: Source,
    client: httpx.Client,
    embedder: Embedder | None,
    store: ChunkStore | None,
    convert: Callable[..., str] = to_markdown,
) -> SourceResult:
    """Ingest one source. Without a store and embedder it's a dry run: fetch, convert, chunk.

    A failure leaves the source's existing chunks in the index untouched.
    """
    result = SourceResult(source.id)
    try:
        # Contact details and identifiers are masked before anything is chunked, embedded
        # or stored; names stay (app/masking.py).
        chunks = chunk_markdown(source, mask_document(convert(fetch(client, source))).text)
        result.chunks = len(chunks)
        if not chunks:
            raise ValueError("No passages after chunking")
        if store is None or embedder is None:
            result.status = "dry run"
            result.added = len(chunks)
            return result

        p = plan(chunks, store.existing_ids(source.id))
        if p.new:
            vectors = embedder.embed([c.embedding_text for c in p.new])
            store.upload([to_document(c, v) for c, v in zip(p.new, vectors, strict=True)])
        if p.stale:
            store.delete(p.stale)
        result.added, result.unchanged, result.removed = len(p.new), p.unchanged, len(p.stale)
        result.sections_added = sorted({c.section for c in p.new})
    except Exception as e:  # one bad source shouldn't stop the others
        result.status, result.error = "failed", f"{type(e).__name__}: {e}"
    return result


def prune(store: ChunkStore, listed: set[str]) -> dict[str, int]:
    removed = {}
    for source_id in sorted(store.source_ids() - listed):
        ids = store.existing_ids(source_id)
        store.delete(ids)
        removed[source_id] = len(ids)
        log.info("%-34s removed: no longer in sources.yaml (%d passages)", source_id, len(ids))
    return removed


def run(
    sources: list[Source],
    skipped: list[Source],
    client: httpx.Client,
    embedder: Embedder | None,
    store: ChunkStore | None,
    convert: Callable[..., str] = to_markdown,
    listed: set[str] | None = None,
) -> RunResult:
    """Ingest the sources. With `listed` (every ID in sources.yaml), also remove the
    passages of any source that is no longer listed."""
    run_result = RunResult(skipped_unverified=[s.id for s in skipped])
    for s in skipped:
        log.info("%-34s skipped: licence unverified (use --include-unverified)", s.id)
    if store is not None:
        run_result.index_created = store.ensure_index()
        if run_result.index_created:
            log.info("Created the search index from search/index.json")
        if listed is not None:
            run_result.pruned = prune(store, listed)
    for source in sources:
        r = ingest_source(source, client, embedder, store, convert)
        run_result.results.append(r)
        if r.status == "failed":
            log.error("%-34s FAILED: %s", r.source_id, r.error)
        else:
            log.info(
                "%-34s %-7s chunks=%4d added=%4d unchanged=%4d removed=%4d",
                r.source_id,
                r.status,
                r.chunks,
                r.added,
                r.unchanged,
                r.removed,
            )
    return run_result


def record(
    result: RunResult, *, started: datetime, duration_s: float, partial: bool
) -> dict[str, Any]:
    """What a run changed, as the drift checks read it (evals/drift.py): each source's
    counts, and the sections of its new or changed passages. Public data only."""
    totals = {
        k: sum(getattr(r, k) for r in result.results)
        for k in ("chunks", "added", "unchanged", "removed")
    }
    return {
        "id": f"{started.strftime('%Y%m%dT%H%M%S')}Z-ingest",
        "kind": "ingest",
        "started_at": started.isoformat(),
        "duration_s": round(duration_s),
        # A run with --only covers some sources: the others weren't checked.
        "partial": partial,
        "index_created": result.index_created,
        "sources": [asdict(r) for r in result.results],
        "pruned": result.pruned,
        "skipped_unverified": result.skipped_unverified,
        "totals": totals,
    }
