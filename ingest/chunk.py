"""Split a document's Markdown into passages of about 200 tokens that keep their heading path."""

import hashlib
import re
from dataclasses import dataclass

from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from ingest.sources import Source

# About 200 tokens of English prose. Characters rather than tokens keep chunking
# offline and free of a tokenizer download; the Phase 2 experiment compares sizes.
CHUNK_CHARS = 800
OVERLAP_CHARS = 100
MIN_CHARS = 60
# A section's whole text is kept if it has this many words, however short: under its
# heading, "The report is only visible to teachers and not students." is an answer.
MIN_WORDS = 5
SECTION_SEPARATOR = " > "

# Sections that only list other sections. They match questions about anything and
# answer none.
SKIP_SECTIONS = {"contents", "table of contents", "on this page"}
# A heading with this many subheadings or more gets a line naming them (add_outlines).
OUTLINE_MIN = 3
OUTLINE_PREFIX = "This section covers: "
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

_HEADERS = [("#" * n, f"h{n}") for n in range(1, 7)]
_by_heading = MarkdownHeaderTextSplitter(_HEADERS, strip_headers=True)
_by_size = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_CHARS,
    chunk_overlap=OVERLAP_CHARS,
    separators=["\n\n", "\n", ". ", "; ", ", ", " ", ""],
    # A sentence keeps its full stop: at the start, a passage began ". Example: …".
    keep_separator="end",
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


def _outline(names: list[str]) -> str | None:
    """The line naming a section's subsections, or None if it shouldn't have one."""
    line = f"{'; '.join(names).rstrip('.')}."
    questions = sum(name.endswith("?") for name in names)
    if len(names) < OUTLINE_MIN or questions * 2 > len(names) or len(line) > CHUNK_CHARS:
        return None
    return line


def add_outlines(markdown: str) -> str:
    """Names each section's subsections at its start, when it has OUTLINE_MIN or more,
    and repeats the main sections' outlines at the end of the page's introduction.

    Search sends at most 4 passages from one source, so an answer about a section with
    seven parts saw two of them and guessed the rest (docs/evaluation.md). The line names
    them all, in the page's own words. A section's own outline seldom ranks, though:
    asked what the AI Assurance Framework asks people to consider, search ranked the
    page's introduction first and the outline below 30th. So the introduction (its
    first section with text) carries the outlines of the sections under the page's top
    heading too. Left out, because they'd match questions they don't answer, as the
    sections in SKIP_SECTIONS do: the top heading's own outline (a whole page's
    contents), a list of questions (an FAQ's, which repeats a visitor's question word for
    word but holds none of its answer), and a line too long for one passage.
    """
    lines = markdown.splitlines()
    headings = [
        (i, len(m.group(1)), m.group(2))
        for i, line in enumerate(lines)
        if (m := _HEADING.match(line))
    ]
    top = min((level for _, level, _ in headings), default=0)
    levels = {i: level for i, level, _ in headings}
    titles = {i: title for i, _, title in headings}
    subsections: dict[int, list[str]] = {}
    parent: dict[int, int | None] = {}
    parents: list[int] = []  # line numbers of the headings above this one
    for i, level, title in headings:
        while parents and levels[parents[-1]] >= level:
            parents.pop()
        parent[i] = parents[-1] if parents else None
        if parents:
            subsections.setdefault(parents[-1], []).append(title)
        parents.append(i)
    outlines = {
        i: line for i in levels if levels[i] > top and (line := _outline(subsections.get(i, [])))
    }
    # The introduction: the first section below the top heading with text of its own.
    starts = sorted(levels)
    ends = dict(zip(starts, [*starts[1:], len(lines)], strict=True))
    intro = next(
        (
            i
            for i in starts
            if levels[i] > top and len(" ".join(lines[i + 1 : ends[i]]).strip()) >= MIN_CHARS
        ),
        None,
    )
    main = [
        f"“{titles[i]}” covers: {line}"
        for i, line in outlines.items()
        if i != intro and (p := parent[i]) is not None and levels[p] == top
    ]
    out = []
    for i, line in enumerate(lines):
        if intro is not None and main and i == ends[intro]:
            out += ["", *main, ""]
        out.append(line)
        if i in outlines:
            out += ["", OUTLINE_PREFIX + outlines[i], ""]
    if intro is not None and main and ends[intro] == len(lines):
        out += ["", *main]
    return "\n".join(out) + "\n"


def chunk_markdown(source: Source, markdown: str) -> list[Chunk]:
    """Passages in document order. Identical passages within a source are kept once.

    A chunk's ID comes from its content hash, so an unchanged passage keeps its ID
    from one run to the next and a changed one gets a new ID.
    """
    chunks: list[Chunk] = []
    seen: set[str] = set()
    for part in _by_heading.split_text(add_outlines(markdown)):
        headings = [part.metadata[key] for _, key in _HEADERS if key in part.metadata]
        if headings and headings[-1].strip().lower() in SKIP_SECTIONS:
            continue
        if source.sections and not any(h in source.sections for h in headings):
            continue
        section = SECTION_SEPARATOR.join(headings)
        pieces = _by_size.split_text(part.page_content)
        for text in pieces:
            text = text.strip()
            whole_answer = (
                len(pieces) == 1
                and bool(headings)
                and not text.startswith(OUTLINE_PREFIX)
                and len(text.split()) >= MIN_WORDS
            )
            if len(text) < MIN_CHARS and not whole_answer:
                continue
            digest = content_hash(source, section, text)
            if digest in seen:
                continue
            seen.add(digest)
            chunks.append(Chunk(f"{source.id}_{digest[:20]}", source, section, text, digest))
    return chunks
