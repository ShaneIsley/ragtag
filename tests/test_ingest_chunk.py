from dataclasses import replace

from ingest.chunk import CHUNK_CHARS, chunk_markdown
from ingest.sources import Source

SENTENCE = "Agencies must keep a register of AI use cases and review it every year. "

DOC = f"""# NSW AI Operational Policy

## Contents

1 Introduction ........ 4
2 Accountability ........ 6

## 5 Standard A: Accountability

### 5.2 Accountable Official

{SENTENCE * 30}

### 5.3 Governance

The Governance Board approves high-risk AI systems before they are deployed.
"""


def test_heading_path_is_kept(source: Source) -> None:
    chunks = chunk_markdown(source, DOC)
    sections = {c.section for c in chunks}
    assert (
        "NSW AI Operational Policy > 5 Standard A: Accountability > 5.2 Accountable Official"
        in sections
    )
    assert "NSW AI Operational Policy > 5 Standard A: Accountability > 5.3 Governance" in sections


def test_contents_sections_are_skipped(source: Source) -> None:
    assert not any("Contents" in c.section for c in chunk_markdown(source, DOC))


def test_long_sections_are_split_to_size(source: Source) -> None:
    chunks = [c for c in chunk_markdown(source, DOC) if c.section.endswith("Accountable Official")]
    assert len(chunks) >= 2
    assert all(len(c.content) <= CHUNK_CHARS for c in chunks)


def test_ids_are_stable_and_follow_content(source: Source) -> None:
    first = chunk_markdown(source, DOC)
    again = chunk_markdown(source, DOC)
    assert [c.id for c in first] == [c.id for c in again]
    assert all(c.id.startswith("test-source_") for c in first)

    changed = chunk_markdown(source, DOC.replace("high-risk", "all high-risk"))
    assert {c.id for c in first} ^ {c.id for c in changed}  # only the edited passage differs
    assert len({c.id for c in first} - {c.id for c in changed}) == 1


def test_licence_change_changes_the_hash(source: Source) -> None:
    before = chunk_markdown(source, DOC)
    after = chunk_markdown(replace(source, licence="CC BY 3.0 AU"), DOC)
    assert {c.content_hash for c in before}.isdisjoint({c.content_hash for c in after})


def test_duplicate_passages_are_kept_once(source: Source) -> None:
    doc = "# Policy\n\n" + ("Report incidents to the Accountable Official within 24 hours.\n\n" * 2)
    assert len(chunk_markdown(source, doc)) == 1


def test_sections_filter_keeps_only_named_subtrees(source: Source) -> None:
    only = replace(source, sections=("5.3 Governance",))
    chunks = chunk_markdown(only, DOC)
    assert chunks
    assert all(c.section.endswith("5.3 Governance") for c in chunks)


def test_embedding_text_includes_title_and_section(source: Source) -> None:
    chunk = chunk_markdown(source, DOC)[-1]
    assert chunk.embedding_text.startswith("Test policy\nNSW AI Operational Policy > ")
    assert chunk.content in chunk.embedding_text
