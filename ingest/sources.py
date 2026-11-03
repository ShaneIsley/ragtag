"""The source list in sources.yaml, and which sources a run includes."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

SOURCES_FILE = Path(__file__).with_name("sources.yaml")


@dataclass(frozen=True)
class Source:
    id: str
    collection: str
    title: str
    url: str
    publisher: str
    published: str
    licence: str
    licence_status: str
    classification: str
    est_chunks: int
    # Headings to keep, for a source that is one part of a larger page. Empty keeps all.
    sections: tuple[str, ...] = field(default=())
    # For a PDF laid out as a two-column table, bold row labels left of this x position
    # (in points) and their text to the right (ingest/convert.py, labelled_rows_to_markdown).
    label_column: float | None = None

    @property
    def unverified(self) -> bool:
        return self.licence_status == "unverified"


def load_sources(path: Path = SOURCES_FILE) -> list[Source]:
    data = yaml.safe_load(path.read_text())
    sources = []
    for s in data["sources"]:
        special = ("est_chunks", "sections", "label_column")
        fields = {k: str(v) for k, v in s.items() if k not in special}
        sources.append(
            Source(
                **fields,
                est_chunks=int(s["est_chunks"]),
                sections=tuple(s.get("sections", ())),
                label_column=float(s["label_column"]) if "label_column" in s else None,
            )
        )
    return sources


def select_sources(
    sources: list[Source], *, include_unverified: bool = False, only: set[str] | None = None
) -> tuple[list[Source], list[Source]]:
    """Split sources into (to ingest, skipped for an unverified licence).

    `only` limits the run to the given IDs; an unknown ID is an error.
    """
    if only:
        unknown = only - {s.id for s in sources}
        if unknown:
            raise ValueError(f"Unknown source IDs: {', '.join(sorted(unknown))}")
        sources = [s for s in sources if s.id in only]
    skipped = [] if include_unverified else [s for s in sources if s.unverified]
    return [s for s in sources if s not in skipped], skipped
