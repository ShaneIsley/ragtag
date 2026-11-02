import pytest

from evals import promptfoo_tests
from evals.golden import load
from evals.promptfoo_tests import generate_tests, runnable
from evals.suites import subsets


def test_every_suite_case_is_a_runnable_golden_case() -> None:
    cases = {c.id: c for c in load().cases}
    for name, ids in subsets().items():
        for case_id in ids:
            assert case_id in cases, f"{name}: {case_id}"
            assert runnable(cases[case_id]), f"{name}: {case_id}"


def test_the_canary_covers_every_category_and_audience_the_full_run_does() -> None:
    runnable_cases = [c for c in load().cases if runnable(c)]
    canary = [c for c in runnable_cases if c.id in subsets()["canary"]]
    assert {c.category for c in canary} == {c.category for c in runnable_cases}
    assert {c.audience for c in canary} >= {"staff", "student", "researcher"}


def test_the_canary_includes_every_known_issue() -> None:
    import yaml

    from evals.gate import THRESHOLDS_FILE

    known = {k["case"] for k in yaml.safe_load(THRESHOLDS_FILE.read_text())["known_issues"]}
    assert known <= subsets()["canary"]


def test_the_standby_suite_asks_only_answerable_questions() -> None:
    cases = {c.id: c for c in load().cases}
    assert all(cases[i].expect.outcome == "answer" for i in subsets()["standby"])


def test_eval_suite_picks_the_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVAL_SUITE", "canary")
    ids = {t["vars"]["case_id"] for t in generate_tests()}
    assert ids == subsets()["canary"]
    monkeypatch.setenv("EVAL_SUITE", "full")
    assert len(generate_tests()) == sum(runnable(c) for c in load().cases)


def test_an_unknown_suite_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVAL_SUITE", "nightly")
    with pytest.raises(ValueError, match="EVAL_SUITE"):
        promptfoo_tests.suite_from_env()
