"""Split a document's Markdown into passages of about 200 tokens that keep their heading path."""

import hashlib
from dataclasses import dataclass

from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from ingest.sources import Source

# About 200 tokens of English prose. Characters rather than tokens keep chunking
# offline and free of a tokenizer download; the Phase 2 experiment compares sizes.
CHUNK_CHARS = 800
OVERLAP_CHARS = 100
MIN_CHARS = 60
SECTION_SEPARATOR = " > "

# Sections that only list other sections. They match questions about anything and
# answer none.
SKIP_SECTIONS = {"contents", "table of contents", "on this page"}

_HEADERS = [("#" * n, f"h{n}") for n in range(1, 7)]
_by_heading = MarkdownHeaderTextSplitter(_HEADERS, strip_headers=True)
_by_size = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_CHARS,
    chunk_overlap=OVERLAP_CHARS,
    separators=["\n\n", "\n", ". ", "; ", ", ", " ", ""],
)


@dataclass(frozen=True)
class Chunk:
    id: str
    source: Source
    section: str
    content: str
    content_hash: str

    @property
    def embedding_text(self) -> str:
        """What gets embedded: the passage with its document and section for context."""
        return f"{self.source.title}\n{self.section}\n\n{self.content}"


def content_hash(source: Source, section: str, content: str) -> str:
    fields = [source.id, source.title, source.url, source.licence, section, content]
    return hashlib.sha256("\x1f".join(fields).encode("utf-8")).hexdigest()


def chunk_markdown(source: Source, markdown: str) -> list[Chunk]:
    """Passages in document order. Identical passages within a source are kept once.

    A chunk's ID comes from its content hash, so an unchanged passage keeps its ID
    from one run to the next and a changed one gets a new ID.
    """
    chunks: list[Chunk] = []
    seen: set[str] = set()
    for part in _by_heading.split_text(markdown):
        headings = [part.metadata[key] for _, key in _HEADERS if key in part.metadata]
        if headings and headings[-1].strip().lower() in SKIP_SECTIONS:
            continue
        if source.sections and not any(h in source.sections for h in headings):
            continue
        section = SECTION_SEPARATOR.join(headings)
        for text in _by_size.split_text(part.page_content):
            text = text.strip()
            if len(text) < MIN_CHARS:
                continue
            digest = content_hash(source, section, text)
            if digest in seen:
                continue
            seen.add(digest)
            chunks.append(Chunk(f"{source.id}_{digest[:20]}", source, section, text, digest))
    return chunks
