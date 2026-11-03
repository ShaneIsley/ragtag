import json
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from ingest.chunk import chunk_markdown
from ingest.fetch import Fetched, make_client
from ingest.pipeline import plan, record, run
from ingest.sources import Source
from ingest.store import to_document

ROOT = Path(__file__).resolve().parents[1]

DOC = """# Policy

## Approval

The Governance Board approves high-risk AI systems before they are deployed.

## Register

Agencies must keep a register of AI use cases and review it every year.
"""


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[0.1] * 1536 for _ in texts]


class FakeStore:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.index_exists = False

    def ensure_index(self) -> bool:
        created = not self.index_exists
        self.index_exists = True
        return created

    def existing_ids(self, source_id: str) -> set[str]:
        return {i for i, d in self.docs.items() if d["source_id"] == source_id}

    def source_ids(self) -> set[str]:
        return {d["source_id"] for d in self.docs.values()}

    def upload(self, documents: Sequence[dict[str, Any]]) -> None:
        self.docs.update({d["id"]: d for d in documents})

    def delete(self, ids: Iterable[str]) -> None:
        for i in ids:
            self.docs.pop(i)


def html_client() -> httpx.Client:
    return make_client(
        httpx.MockTransport(
            lambda r: httpx.Response(
                200, content=b"<main>x</main>", headers={"content-type": "text/html"}
            )
        )
    )


def converter(markdown: str) -> Any:
    def convert(doc: Fetched) -> str:
        return markdown

    return convert


def test_plan(source: Source) -> None:
    chunks = chunk_markdown(source, DOC)
    p = plan(chunks, {chunks[0].id, "test-source_gone"})
    assert [c.id for c in p.new] == [chunks[1].id]
    assert p.unchanged == 1
    assert p.stale == {"test-source_gone"}


def test_first_run_creates_index_and_uploads(source: Source) -> None:
    store, embedder = FakeStore(), FakeEmbedder()
    result = run([source], [], html_client(), embedder, store, converter(DOC))
    assert result.index_created
    assert result.results[0].added == 2
    assert len(store.docs) == 2
    assert len(embedder.calls) == 1


def test_second_run_skips_unchanged_chunks(source: Source) -> None:
    store, embedder = FakeStore(), FakeEmbedder()
    run([source], [], html_client(), embedder, store, converter(DOC))
    result = run([source], [], html_client(), embedder, store, converter(DOC))
    r = result.results[0]
    assert (r.added, r.unchanged, r.removed) == (0, 2, 0)
    assert len(embedder.calls) == 1  # nothing re-embedded
    assert not result.index_created


def test_changed_passage_is_replaced(source: Source) -> None:
    store, embedder = FakeStore(), FakeEmbedder()
    run([source], [], html_client(), embedder, store, converter(DOC))
    edited = DOC.replace("every year", "every six months")
    r = run([source], [], html_client(), embedder, store, converter(edited)).results[0]
    assert (r.added, r.unchanged, r.removed) == (1, 1, 1)
    assert any("six months" in d["content"] for d in store.docs.values())
    assert not any("every year" in d["content"] for d in store.docs.values())


def test_a_run_records_what_changed_for_the_drift_checks(source: Source) -> None:
    store, embedder = FakeStore(), FakeEmbedder()
    run([source], [], html_client(), embedder, store, converter(DOC))
    edited = DOC.replace("every year", "every six months")
    result = run([source], [], html_client(), embedder, store, converter(edited))
    started = datetime(2026, 11, 2, 16, 0, tzinfo=UTC)
    summary = record(result, started=started, duration_s=177.4, partial=False)
    assert summary["id"] == "20261102T160000Z-ingest"
    assert summary["totals"] == {"chunks": 2, "added": 1, "unchanged": 1, "removed": 1}
    (changed,) = summary["sources"]
    assert changed["sections_added"] == ["Policy > Register"]
    assert json.loads(json.dumps(summary)) == summary


def test_the_record_goes_to_the_bucket_and_a_failure_doesn_t_fail_the_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from ingest.__main__ import publish

    class FakeS3:
        def __init__(self, fail: bool = False) -> None:
            self.fail = fail
            self.keys: list[str] = []

        def put_object(self, Bucket: str, Key: str, **_: Any) -> None:
            if self.fail:
                raise RuntimeError("AccessDenied")
            self.keys.append(Key)

    s3 = FakeS3()
    publish({"id": "20261102T160000Z-ingest"}, "records", s3)
    assert s3.keys == ["ingest/20261102T160000Z-ingest.json"]
    publish({"id": "x"}, "records", FakeS3(fail=True))
    assert "Couldn't record the run" in caplog.text


def test_failed_source_keeps_its_chunks_and_others_continue(source: Source) -> None:
    store, embedder = FakeStore(), FakeEmbedder()
    run([source], [], html_client(), embedder, store, converter(DOC))
    other = replace(source, id="other-source")

    def flaky(doc: Fetched) -> str:
        if doc.source.id == source.id:
            raise RuntimeError("site down")
        return DOC

    result = run([source, other], [], html_client(), embedder, store, flaky)
    assert [r.status for r in result.results] == ["failed", "ok"]
    assert "site down" in result.results[0].error
    assert len(store.existing_ids(source.id)) == 2


def test_dry_run_touches_nothing(source: Source) -> None:
    result = run([source], [], html_client(), None, None, converter(DOC))
    assert result.results[0].status == "dry run"
    assert result.results[0].chunks == 2


def test_document_fields_match_the_index_definition(source: Source) -> None:
    index = json.loads((ROOT / "search" / "index.json").read_text())
    chunk = chunk_markdown(source, DOC)[0]
    doc = to_document(chunk, [0.0] * 1536)
    assert set(doc) == {f["name"] for f in index["fields"]}


def test_sources_no_longer_listed_are_removed(source: Source) -> None:
    store, embedder = FakeStore(), FakeEmbedder()
    dropped = replace(source, id="dropped-source")
    run([source, dropped], [], html_client(), embedder, store, converter(DOC))
    result = run([source], [], html_client(), embedder, store, converter(DOC), listed={source.id})
    assert result.pruned == {"dropped-source": 2}
    assert store.source_ids() == {source.id}


def test_unlisted_run_prunes_nothing(source: Source) -> None:
    store, embedder = FakeStore(), FakeEmbedder()
    other = replace(source, id="other-source")
    run([source, other], [], html_client(), embedder, store, converter(DOC))
    result = run([source], [], html_client(), embedder, store, converter(DOC))
    assert result.pruned == {}
    assert store.source_ids() == {source.id, "other-source"}


def test_lambda_handler_fails_the_invocation_when_a_source_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ingest import lambda_handler

    monkeypatch.setattr(lambda_handler, "main", lambda argv: 1)
    with pytest.raises(RuntimeError, match="failed sources"):
        lambda_handler.handler({}, None)
    monkeypatch.setattr(lambda_handler, "main", lambda argv: 0)
    assert lambda_handler.handler({}, None) == {"status": "ok"}


def test_documents_are_masked_before_they_are_stored(source: Source) -> None:
    store, embedder = FakeStore(), FakeEmbedder()
    doc = (
        "# Contacts\n\nProfessor Jane Smith chairs the committee. Email "
        "integrity@unsw.edu.au with questions about the process for reviews."
    )
    run([source], [], html_client(), embedder, store, converter(doc))
    stored = " ".join(d["content"] for d in store.docs.values())
    assert "integrity@unsw.edu.au" not in stored and "[email address]" in stored
    assert "Professor Jane Smith" in stored
    assert not any("integrity@unsw.edu.au" in t for call in embedder.calls for t in call)
