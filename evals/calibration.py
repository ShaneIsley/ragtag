"""Judge calibration: the evaluation's three judges score fixed answers whose verdicts
are known (evals/calibration.yaml), so a change in a judge shows apart from a change
in the service.

Each answer says what each judge should conclude: the facts judge's verdict on every
expected fact, and a floor or ceiling on the groundedness and relevance graders'
scores. A run counts how many of those expectations each judge meets (its agreement),
and records it as a calibration run. `minimum_agreement` in the YAML sets the floor
for each judge; until a baseline sets it, runs are recorded only.

Run it with `make calibrate`, or on AWS with the nightly schedule (evals/lambda_handler.py).
"""

import argparse
import json
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from evals import records
from evals.facts import Verdict
from evals.golden import Case, load

CALIBRATION_FILE = Path(__file__).with_name("calibration.yaml")
JUDGES = ("facts", "groundedness", "relevance")
AnswerKind = Literal["good", "omits", "contradicts", "unsupported", "off_topic"]


class Bound(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    at_least: float | None = None
    at_most: float | None = None

    @model_validator(mode="after")
    def _one(self) -> Self:
        if (self.at_least is None) == (self.at_most is None):
            raise ValueError("give either at_least or at_most")
        return self

    def met(self, score: float) -> bool:
        if self.at_least is not None:
            return score >= self.at_least
        return self.at_most is not None and score <= self.at_most


class Expect(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    facts: list[Verdict] | None = None
    groundedness: Bound | None = None
    relevance: Bound | None = None


class Passage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    number: int
    source_id: str
    section: str
    content: str


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: AnswerKind
    expect: Expect
    text: str


class CalibrationCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    case: str
    passages: list[Passage]
    answers: list[Answer]


class CalibrationSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str
    minimum_agreement: dict[str, float] | None = None
    cases: list[CalibrationCase] = Field(min_length=1)


def load_set(path: Path = CALIBRATION_FILE) -> CalibrationSet:
    """The calibration set, checked against the golden set: every case exists, and
    every facts expectation has one verdict per expected fact."""
    calibration = CalibrationSet.model_validate(yaml.safe_load(path.read_text()))
    golden = {c.id: c for c in load().cases}
    for c in calibration.cases:
        if c.case not in golden:
            raise ValueError(f"{c.case} isn't a golden case")
        count = len(golden[c.case].expect.facts)
        for a in c.answers:
            if a.expect.facts is not None and len(a.expect.facts) != count:
                expected = len(a.expect.facts)
                raise ValueError(f"{c.case} {a.kind}: {count} facts, but {expected} verdicts")
    for judge in calibration.minimum_agreement or {}:
        if judge not in JUDGES:
            raise ValueError(f"minimum_agreement: unknown judge {judge}")
    return calibration


@dataclass(frozen=True)
class Judges:
    """The three judges, as calibration calls them. `models` counts the model versions
    the facts judge's replies came from, since a new version is a likely cause of drift."""

    facts: Callable[[list[str], str], list[str]]
    groundedness: Callable[[str, str, str], float]
    relevance: Callable[[str, str], float]
    models: Counter[str] = field(default_factory=Counter)


def azure_judges() -> Judges:
    """The judges the gate uses, on the app's chat deployment."""
    from app.answer import AzureChat, Completion, Effort
    from app.settings import get_settings
    from evals import facts, graders

    settings = get_settings()
    chat = AzureChat(settings)
    models: Counter[str] = Counter()

    class Counted:
        def complete(self, system: str, user: str, *, effort: Effort = "low") -> Completion:
            completion = chat.complete(system, user, effort=effort)
            models[completion.model] += 1
            return completion

    return Judges(
        facts=lambda expected, answer: [
            v.verdict for v in facts.judge(expected, answer, Counted())
        ],
        groundedness=lambda q, a, ctx: graders.groundedness(settings, q, a, ctx).score,
        relevance=lambda q, a: graders.relevance(settings, q, a).score,
        models=models,
    )


def context(passages: Sequence[Passage]) -> str:
    """The passages as the groundedness grader sees an answer's cited sources."""
    return "\n\n".join(f"[{p.number}] {p.content}" for p in passages)


@dataclass
class Tally:
    met: int = 0
    expected: int = 0
    misses: list[str] = field(default_factory=list)

    def add(self, ok: bool, what: str) -> None:
        self.expected += 1
        self.met += ok
        if not ok:
            self.misses.append(what)

    @property
    def agreement(self) -> float | None:
        return self.met / self.expected if self.expected else None


def judge_answer(
    case: Case, c: CalibrationCase, a: Answer, judges: Judges, tallies: Mapping[str, Tally]
) -> dict[str, Any]:
    """Asks each judge the answer expects a verdict from; returns what they said."""
    label = f"{c.case} {a.kind}"
    question = case.last_question
    result: dict[str, Any] = {"case": c.case, "kind": a.kind}
    if a.expect.facts is not None:
        try:
            got = judges.facts(case.expect.facts, a.text)
        except ValueError as e:
            got = []
            result["facts_error"] = str(e)
        result["facts"] = got
        for n, want in enumerate(a.expect.facts, start=1):
            have = got[n - 1] if n <= len(got) else "no verdict"
            tallies["facts"].add(have == want, f"{label}: fact {n} {have}, expected {want}")
    if a.expect.groundedness is not None:
        score = judges.groundedness(question, a.text, context(c.passages))
        result["groundedness"] = score
        tallies["groundedness"].add(
            a.expect.groundedness.met(score), f"{label}: groundedness {score:g}"
        )
    if a.expect.relevance is not None:
        score = judges.relevance(question, a.text)
        result["relevance"] = score
        tallies["relevance"].add(a.expect.relevance.met(score), f"{label}: relevance {score:g}")
    return result


def run(
    calibration: CalibrationSet, judges: Judges
) -> tuple[dict[str, Tally], list[dict[str, Any]]]:
    golden = {c.id: c for c in load().cases}
    tallies = {name: Tally() for name in JUDGES}
    results = [
        judge_answer(golden[c.case], c, a, judges, tallies)
        for c in calibration.cases
        for a in c.answers
    ]
    return tallies, results


def scores_by_kind(results: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
    """The mean groundedness and relevance score for each kind of answer: the numbers
    whose drift the trend shows."""
    by: dict[str, dict[str, list[float]]] = {}
    for r in results:
        for judge in ("groundedness", "relevance"):
            if judge in r:
                by.setdefault(judge, {}).setdefault(r["kind"], []).append(float(r[judge]))
    return {j: {k: round(sum(v) / len(v), 3) for k, v in kinds.items()} for j, kinds in by.items()}


def verdict(tallies: Mapping[str, Tally], minimums: Mapping[str, float] | None) -> list[str]:
    """Failures: a judge below its minimum agreement. None until minimums are set."""
    failures = []
    for judge, minimum in (minimums or {}).items():
        agreement = tallies[judge].agreement
        if agreement is not None and agreement < minimum:
            failures.append(f"{judge} agreement {agreement:.3f} is below its minimum {minimum}")
    return failures


def build_record(
    calibration: CalibrationSet,
    tallies: Mapping[str, Tally],
    results: Sequence[Mapping[str, Any]],
    failures: Sequence[str],
    *,
    started: datetime,
    duration_s: float,
    models: Mapping[str, int] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": f"{started.strftime('%Y%m%dT%H%M%S')}Z-calibration-calibration",
        "kind": "calibration",
        "suite": "calibration",
        "started_at": started.isoformat(),
        "duration_s": round(duration_s),
        # The judges are measured, not the service, so there's no target.
        "target": None,
        "provider": "azure",
        "measured": {"commit": records.commit(), "judge_model": dict(models or {})},
        "measured_with": {
            "calibration_set": records.file_hash(("evals/calibration.yaml",)),
            "golden_set": records.file_hash(("evals/golden.yaml",)),
            "graders": records.file_hash(records.MEASURED_WITH["graders"]),
        },
        "passed": not failures,
        "failures": list(failures),
        "warnings": [],
        "summary": {
            "version": calibration.version,
            "agreement": {j: t.agreement for j, t in tallies.items()},
            "expectations": {j: t.expected for j, t in tallies.items()},
            "misses": {j: t.misses for j, t in tallies.items() if t.misses},
            "scores_by_kind": scores_by_kind(results),
        },
    }
    if note:
        record["note"] = note
    return record


def main(argv: Sequence[str], judges: Judges | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.calibration")
    parser.add_argument("--note", help="why this run was made, for the record")
    parser.add_argument("--no-record", action="store_true", help="don't save a run record")
    args = parser.parse_args(argv)

    calibration = load_set()
    started, clock = datetime.now(UTC), time.monotonic()
    judges = judges or azure_judges()
    tallies, results = run(calibration, judges)
    failures = verdict(tallies, calibration.minimum_agreement)
    minimums = calibration.minimum_agreement or {}
    answers = sum(len(c.answers) for c in calibration.cases)
    print(f"calibration {calibration.version}: {answers} answers")
    for judge, tally in tallies.items():
        floor = minimums.get(judge, "none yet")
        print(f"  {judge:13} {tally.met} of {tally.expected} expectations met  (minimum {floor})")
        for miss in tally.misses:
            print(f"    MISS {miss}")
    for failure in failures:
        print(f"FAIL {failure}")
    print("Calibration: FAILED" if failures else "Calibration: passed")

    if not args.no_record:
        record = build_record(
            calibration,
            tallies,
            results,
            failures,
            started=started,
            duration_s=time.monotonic() - clock,
            models=judges.models,
            note=args.note,
        )
        path = records.save(record)
        kept = path.with_suffix(".results.json")
        kept.write_text(json.dumps(results, indent=1))
        records.keep_results(kept, path)
        kept.unlink()
        print(f"Recorded as a calibration run: {path}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
