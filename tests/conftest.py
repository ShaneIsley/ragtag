from dataclasses import replace

import pytest

from ingest.sources import Source

SOURCE = Source(
    id="test-source",
    collection="Test",
    title="Test policy",
    url="https://example.gov.au/policy.html",
    publisher="Test publisher",
    published="Oct 2026",
    licence="CC BY 4.0",
    licence_status="open licence",
    classification="Public",
    est_chunks=3,
)


@pytest.fixture
def source() -> Source:
    return SOURCE


@pytest.fixture
def pdf_source() -> Source:
    return replace(SOURCE, id="test-pdf", url="https://example.gov.au/policy.pdf")
