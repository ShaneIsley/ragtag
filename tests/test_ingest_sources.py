from dataclasses import replace

import pytest

from ingest.sources import load_sources, select_sources


def test_sources_file_loads() -> None:
    sources = load_sources()
    assert len(sources) == 17
    assert len({s.id for s in sources}) == 17
    assert all(s.id.startswith("unsw-") and s.publisher.startswith("UNSW") for s in sources)
    assert not any(s.unverified for s in sources)


def test_unverified_sources_are_skipped_by_default() -> None:
    sources = load_sources()
    unverified = replace(sources[0], id="unsw-unverified", licence_status="unverified")
    included, skipped = select_sources([*sources, unverified])
    assert included == sources
    assert skipped == [unverified]
    included, skipped = select_sources([*sources, unverified], include_unverified=True)
    assert (len(included), skipped) == (18, [])


def test_only_limits_the_run() -> None:
    included, skipped = select_sources(load_sources(), only={"unsw-myit-ai", "unsw-using-copilot"})
    assert [s.id for s in included] == ["unsw-myit-ai", "unsw-using-copilot"]
    assert skipped == []


def test_only_rejects_unknown_ids() -> None:
    with pytest.raises(ValueError, match="no-such-source"):
        select_sources(load_sources(), only={"no-such-source"})


def test_sections_filter_loads() -> None:
    by_id = {s.id: s for s in load_sources()}
    assert by_id["unsw-ig-policy-ai"].sections == (
        "6. Use of AI systems or tools",
        "Section 6: Use of Artificial Intelligence Systems or Tools Procedure",
    )
    assert by_id["unsw-code-of-conduct"].sections[0].startswith("3.2. We are accurate")
    assert by_id["unsw-myit-ai"].sections == ()


def test_label_column_loads_as_a_number() -> None:
    by_id = {s.id: s for s in load_sources()}
    assert by_id["unsw-ai-legal-fact-sheet"].label_column == 140.0
    assert by_id["unsw-ai-responsible-research"].label_column is None
