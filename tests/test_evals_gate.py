from typing import Any

import pytest

from evals.gate import NOT_APPLICABLE, failures, p90, summarise, verdict, warnings

THRESHOLDS = {
    "measures": {
        "facts": {"minimum": 0.7},
        "relevance": {"minimum": 0.8, "exclude_categories": ["injection"]},
    },
    "latency": {"p90_seconds_maximum": 8.0},
}


def row(case_id: str, latency_ms: float, *checks: tuple[str, bool, float, str]) -> dict[str, Any]:
    return {
        "vars": {"case_id": case_id},
        "gradingResult": {
            "componentResults": [
                {"assertion": {"metric": m}, "pass": p, "score": s, "reason": r}
                for m, p, s, r in checks
            ]
        },
        "response": {"metadata": {"latency_ms": latency_ms}},
    }


def results(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"results": {"results": list(rows)}}


CATEGORIES = {"a": "in_scope", "b": "in_scope", "c": "injection"}


def test_measures_are_averaged_over_the_cases_they_apply_to() -> None:
    run = results(
        row("a", 3000, ("facts", True, 1.0, "2 of 2"), ("relevance", True, 1.0, "5 of 5")),
        row("b", 5000, ("facts", True, 0.5, "1 of 2"), ("relevance", True, 0.8, "4 of 5")),
        row("c", 4000, ("facts", True, 1.0, NOT_APPLICABLE), ("relevance", True, 0.6, "3 of 5")),
    )
    summary = summarise(run, CATEGORIES, {"relevance": frozenset({"injection"})})
    assert summary.means["facts"] == pytest.approx(0.75)  # c's facts don't apply
    assert summary.means["relevance"] == pytest.approx(0.9)  # c is an injection case
    assert summary.counts == {
        "facts": 2,
        "groundedness": 0,
        "relevance": 2,
        "recall": 0,
        "cites_expected": 0,
    }
    assert failures(summary, THRESHOLDS) == []


def test_a_rule_failure_an_error_a_low_measure_and_slow_answers_all_fail_the_run() -> None:
    run = results(
        row(
            "a",
            9000,
            ("outcome", False, 0.0, "expected answer, declined"),
            ("facts", True, 0.5, ""),
        ),
        {"vars": {"case_id": "b"}, "error": "HTTP 503"},
    )
    found = failures(summarise(run, CATEGORIES), THRESHOLDS)
    assert any(f.startswith("rule failed: a outcome") for f in found)
    assert any(f.startswith("error: b") for f in found)
    assert any(f.startswith("facts: mean 0.500") for f in found)
    assert any(f.startswith("relevance: no scores") for f in found)
    assert any(f.startswith("latency: 90th percentile 9.0 s") for f in found)


def test_p90_is_the_value_nine_tenths_of_the_way_up() -> None:
    assert p90([float(n) for n in range(1, 11)]) == 9.0
    assert p90([]) == 0.0


def test_a_known_issue_warns_instead_of_failing() -> None:
    run = results(
        row("a", 3000, ("outcome", False, 0.0, "declined"), ("facts", True, 1.0, "")),
        row("b", 3000, ("facts", False, 0.0, "contradicted"), ("relevance", True, 1.0, "")),
    )
    thresholds = THRESHOLDS | {"known_issues": [{"case": "a", "check": "outcome", "why": "flaky"}]}
    summary = summarise(run, CATEGORIES)
    assert warnings(summary, thresholds) == ["known issue: a outcome: declined"]
    found = failures(summary, thresholds)
    assert [f for f in found if f.startswith("rule failed")] == [
        "rule failed: b facts: contradicted"
    ]


LOW_FACTS = results(row("a", 1000, ("outcome", True, 1.0, ""), ("facts", True, 0.5, "")))


def test_a_full_run_fails_on_a_low_measure() -> None:
    found, _ = verdict(summarise(LOW_FACTS, CATEGORIES), THRESHOLDS, "full")
    assert any(f.startswith("facts: mean 0.500") for f in found)


def test_a_canary_only_warns_on_a_low_measure() -> None:
    found, warned = verdict(summarise(LOW_FACTS, CATEGORIES), THRESHOLDS, "canary")
    assert found == []
    assert any(w.startswith("facts: mean 0.500") and "canary" in w for w in warned)


def test_a_standby_run_records_its_measures_without_judging_them() -> None:
    found, warned = verdict(summarise(LOW_FACTS, CATEGORIES), THRESHOLDS, "standby")
    assert found == [] and warned == []


def test_a_rule_failure_fails_every_suite() -> None:
    run = results(row("a", 1000, ("outcome", False, 0.0, "expected answer, declined")))
    for suite in ("full", "canary", "standby"):
        found, _ = verdict(summarise(run, CATEGORIES), THRESHOLDS, suite)  # type: ignore[arg-type]
        assert any(f.startswith("rule failed: a outcome") for f in found), suite


def test_a_run_that_never_reached_the_app_isnt_recorded(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from evals import gate, records

    monkeypatch.setattr(records, "RECORDS_DIR", tmp_path / "records")
    run = results(
        {"vars": {"case_id": "a"}, "error": "Couldn't reach the app"},
        {"vars": {"case_id": "b"}, "error": "Couldn't reach the app"},
    )
    path = tmp_path / "latest.json"
    path.write_text(json.dumps(run))
    assert gate.main([str(path), "--kind", "release"]) == 1
    assert "Not recorded" in capsys.readouterr().out
    assert not (tmp_path / "records").exists()
