"""The golden set in golden.yaml: questions with what a correct response must do.

The evaluation gate (Phase 2 step 2) loads these cases and calls the running app.
This module only describes and checks the file; it calls nothing.
"""

from datetime import date
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

GOLDEN_FILE = Path(__file__).with_name("golden.yaml")

Category = Literal[
    "in_scope",  # answerable from UNSW's documents
    "not_covered",  # about AI, but UNSW's documents don't answer it: must decline
    "off_topic",  # not about AI at UNSW: must decline
    "personal_details",  # answerable, but the question holds made-up personal details
    "injection",  # the question tries to change the rules
    "conversation",  # several turns; the expectations apply to the last answer
]
Audience = Literal["staff", "student", "researcher", "any"]
Capability = Literal["conversation_memory"]

DECLINE_CATEGORIES: frozenset[str] = frozenset({"not_covered", "off_topic"})


class Expect(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Literal["answer", "decline"]
    # Every source whose passages support a correct answer (IDs from ingest/sources.yaml).
    sources: list[str] = Field(default_factory=list)
    # Short statements a correct answer must make, worded from the passages.
    facts: list[str] = Field(default_factory=list)
    # Text the answer must not contain: personal details from the question, or the
    # output an injection asks for.
    must_not_contain: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.outcome == "decline" and (self.sources or self.facts):
            raise ValueError("a decline expects no sources or facts")
        if self.outcome == "answer" and not (self.sources and self.facts):
            raise ValueError("an answer needs at least one source and one fact")
        return self


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    category: Category
    audience: Audience = "any"
    # A single question, or the turns of a conversation (never both).
    question: str | None = None
    turns: list[str] | None = None
    # What the app must support before this case can run.
    requires: list[Capability] = Field(default_factory=list)
    expect: Expect
    # When the expectations need checking against the documents again, for cases
    # whose facts depend on a date.
    review_by: date | None = None
    notes: str = ""

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if (self.question is None) == (self.turns is None):
            raise ValueError(f"{self.id}: give either question or turns")
        if self.category == "conversation":
            if not self.turns or len(self.turns) < 2:
                raise ValueError(f"{self.id}: a conversation needs at least two turns")
            if "conversation_memory" not in self.requires:
                raise ValueError(f"{self.id}: a conversation requires conversation_memory")
        elif self.turns is not None:
            raise ValueError(f"{self.id}: only conversations have turns")
        if self.category in DECLINE_CATEGORIES and self.expect.outcome != "decline":
            raise ValueError(f"{self.id}: {self.category} cases must expect a decline")
        return self

    @property
    def last_question(self) -> str:
        return self.turns[-1] if self.turns else (self.question or "")


class GoldenSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    cases: list[Case]

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [c.id for c in self.cases]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate case IDs: {', '.join(duplicates)}")
        return self


def load(path: Path = GOLDEN_FILE) -> GoldenSet:
    return GoldenSet.model_validate(yaml.safe_load(path.read_text()))


def overdue(golden: GoldenSet, today: date) -> list[Case]:
    """Cases whose review date has passed."""
    return [c for c in golden.cases if c.review_by is not None and c.review_by <= today]
