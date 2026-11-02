from typing import Any, get_args

import pytest
from pydantic import ValidationError

from evals.golden import Case, Category, GoldenSet, load
from ingest.sources import load_sources

GOLDEN = load()


def test_golden_set_is_the_planned_size() -> None:
    assert 35 <= len(GOLDEN.cases) <= 50


def test_every_category_is_present() -> None:
    assert {c.category for c in GOLDEN.cases} == set(get_args(Category))


def test_in_scope_cases_cover_staff_students_and_researchers() -> None:
    audiences = {c.audience for c in GOLDEN.cases if c.category == "in_scope"}
    assert {"staff", "student", "researcher"} <= audiences


def test_expected_sources_exist() -> None:
    known = {s.id for s in load_sources()}
    unknown = {s for c in GOLDEN.cases for s in c.expect.sources} - known
    assert not unknown


def test_declines_expect_no_sources() -> None:
    declines = [c for c in GOLDEN.cases if c.expect.outcome == "decline"]
    assert declines
    assert all(not c.expect.sources and not c.expect.facts for c in declines)


def test_conversations_have_several_turns() -> None:
    conversations = [c for c in GOLDEN.cases if c.category == "conversation"]
    assert len(conversations) == 6
    assert all(c.turns and len(c.turns) >= 2 for c in conversations)


def test_personal_details_appear_in_the_question() -> None:
    # Otherwise must_not_contain would check nothing.
    for c in GOLDEN.cases:
        if c.category == "personal_details":
            assert c.expect.must_not_contain
            assert all(d in c.last_question for d in c.expect.must_not_contain), c.id


def case(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "a-case",
        "category": "in_scope",
        "question": "What is Scout?",
        "expect": {"outcome": "answer", "sources": ["unsw-teaching-scout"], "facts": ["x"]},
    }
    return base | overrides


def test_a_well_formed_case_loads() -> None:
    assert Case.model_validate(case()).last_question == "What is Scout?"


@pytest.mark.parametrize(
    "overrides",
    [
        {"expect": {"outcome": "decline", "sources": ["unsw-teaching-scout"]}},
        {"expect": {"outcome": "answer"}},
        {"category": "off_topic"},
        {"turns": ["one", "two"]},
        {"question": None},
        {"category": "conversation", "question": None, "turns": ["only one"]},
        {"category": "conversation", "question": None, "turns": ["one", "two"]},
        {"id": "Not Kebab"},
        {"unknown_field": 1},
    ],
)
def test_malformed_cases_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Case.model_validate(case(**overrides))


def test_duplicate_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        GoldenSet.model_validate({"version": "test", "cases": [case(), case()]})


def test_overdue_lists_cases_whose_review_date_has_passed() -> None:
    from datetime import date

    from evals.golden import load, overdue

    golden = load()
    dated = [c for c in golden.cases if c.review_by is not None]
    assert dated, "the assessment-modes case carries a review date"
    assert overdue(golden, date(2026, 10, 31)) == []
    assert {c.id for c in overdue(golden, date(2027, 3, 1))} == {c.id for c in dated}
