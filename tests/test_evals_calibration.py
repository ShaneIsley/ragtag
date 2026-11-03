import gzip
import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from evals import calibration, records
from evals.calibration import Bound, Judges, Tally
from evals.golden import load

CALIBRATION = calibration.load_set()
GOLDEN = {c.id: c for c in load().cases}


def test_the_calibration_set_loads_and_each_case_has_every_kind_of_answer() -> None:
    kinds = {"good", "omits", "contradicts", "unsupported", "off_topic"}
    assert len(CALIBRATION.cases) == 5
    for c in CALIBRATION.cases:
        assert {a.kind for a in c.answers} == kinds
        good = next(a for a in c.answers if a.kind == "good")
        assert good.expect.facts == ["stated"] * len(GOLDEN[c.case].expect.facts)


def test_answers_cite_only_their_case_s_passages() -> None:
    for c in CALIBRATION.cases:
        numbers = {p.number for p in c.passages}
        for a in c.answers:
            cited = {int(n) for n in re.findall(r"\[(\d+)\]", a.text)}
            assert cited <= numbers, f"{c.case} {a.kind} cites {cited - numbers}"
            assert (a.kind == "off_topic") == (not cited)


def test_a_facts_expectation_must_match_the_golden_case(tmp_path: Path) -> None:
    text = calibration.CALIBRATION_FILE.read_text().replace(
        "facts: [stated, stated, stated]", "facts: [stated, stated]", 1
    )
    bad = tmp_path / "calibration.yaml"
    bad.write_text(text)
    with pytest.raises(ValueError, match="3 facts, but 2 verdicts"):
        calibration.load_set(bad)


def test_a_bound_is_a_floor_or_a_ceiling() -> None:
    assert Bound(at_least=4).met(4) and not Bound(at_least=4).met(3)
    assert Bound(at_most=2).met(2) and not Bound(at_most=2).met(3)
    with pytest.raises(ValueError):
        Bound()


def expected_judges() -> Judges:
    """Judges that give every expected verdict, read from the set itself."""
    by_text = {a.text: a.expect for c in CALIBRATION.cases for a in c.answers}

    def score(name: str) -> Callable[..., float]:
        def judge(*args: str) -> float:
            bound = getattr(by_text[args[1]], name)
            return float(bound.at_least if bound.at_least is not None else bound.at_most)

        return judge

    return Judges(
        facts=lambda expected, answer: list(by_text[answer].facts or []),
        groundedness=score("groundedness"),
        relevance=score("relevance"),
    )


def test_judges_that_give_the_expected_verdicts_agree_completely() -> None:
    tallies, results = calibration.run(CALIBRATION, expected_judges())
    assert {j: t.agreement for j, t in tallies.items()} == {
        "facts": 1.0,
        "groundedness": 1.0,
        "relevance": 1.0,
    }
    # Per case: facts on 3 answers, groundedness on 3, relevance on 2.
    assert len(results) == 25


def test_a_judge_that_finds_every_fact_stated_misses_the_bad_answers() -> None:
    judges = expected_judges()
    lenient = Judges(
        facts=lambda expected, answer: ["stated"] * len(expected),
        groundedness=judges.groundedness,
        relevance=judges.relevance,
    )
    tallies, _ = calibration.run(CALIBRATION, lenient)
    facts = tallies["facts"]
    # 12 facts across the five cases, judged on 3 answers each; one omitted or
    # contradicted fact per bad answer, and the brainstorming contradiction drops two.
    assert facts.expected == 36
    assert facts.expected - facts.met == 11
    assert any("contradicts: fact 1 stated, expected contradicted" in m for m in facts.misses)


def test_a_run_fails_only_when_a_judge_is_below_a_set_minimum() -> None:
    tallies = {
        "facts": Tally(met=8, expected=10),
        "groundedness": Tally(1, 1),
        "relevance": Tally(1, 1),
    }
    assert calibration.verdict(tallies, None) == []
    assert calibration.verdict(tallies, {"facts": 0.8}) == []
    assert calibration.verdict(tallies, {"facts": 0.9}) == [
        "facts agreement 0.800 is below its minimum 0.9"
    ]


def test_main_records_a_calibration_run_with_its_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(records, "RECORDS_DIR", tmp_path)
    monkeypatch.delenv("EVAL_RECORDS_BUCKET", raising=False)
    judges = expected_judges()
    judges.models["gpt-5-mini-2025-08-07"] += 1
    assert calibration.main(["--note", "test"], judges=judges) == 0
    (path,) = tmp_path.glob("*-calibration-calibration.json")
    record = json.loads(path.read_text())
    assert record["kind"] == "calibration" and record["passed"] is True
    assert record["target"] is None
    assert record["measured"]["judge_model"] == {"gpt-5-mini-2025-08-07": 1}
    assert record["summary"]["agreement"]["facts"] == 1.0
    assert set(record["measured_with"]) == {"calibration_set", "golden_set", "graders"}
    kept = tmp_path / "results" / f"{path.stem}.json.gz"
    assert len(json.loads(gzip.decompress(kept.read_bytes()))) == 25
    assert not list(tmp_path.glob("*.results.json"))
