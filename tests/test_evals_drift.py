import io
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from evals import drift
from evals.drift import Signal

NOW = datetime(2026, 11, 16, 18, 0, tzinfo=UTC)


def run(
    day: int, suite: str = "full", model: str = "gpt-5-mini-2025-08-07", **means: float
) -> dict[str, Any]:
    return {
        "id": f"202611{day:02d}-{suite}",
        "suite": suite,
        "started_at": datetime(2026, 11, day, 17, tzinfo=UTC).isoformat(),
        "passed": True,
        "measured": {"model": {model: 30}},
        "summary": {"means": means},
    }


def calibration(
    day: int, model: str = "gpt-5-mini-2025-08-07", **agreement: float
) -> dict[str, Any]:
    return {
        "id": f"202611{day:02d}-calibration",
        "started_at": datetime(2026, 11, day, 15, tzinfo=UTC).isoformat(),
        "measured": {"judge_model": {model: 15}},
        "summary": {"agreement": agreement},
    }


def ingest(day: int, *sources: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    return {
        "id": f"202611{day:02d}-ingest",
        "started_at": datetime(2026, 11, day, 16, tzinfo=UTC).isoformat(),
        "partial": partial,
        "sources": list(sources),
        "pruned": {},
    }


def source(source_id: str, added: int = 0, removed: int = 0, status: str = "ok") -> dict[str, Any]:
    return {"source_id": source_id, "status": status, "added": added, "removed": removed}


class FakeStore:
    def __init__(self, **prefixes: list[dict[str, Any]]) -> None:
        self.prefixes = prefixes
        self.objects: dict[str, Any] = {}

    def records(self, prefix: str) -> list[dict[str, Any]]:
        return self.prefixes.get(prefix.strip("/").replace("/", "_").replace("-", "_"), [])

    def get(self, key: str) -> dict[str, Any] | None:
        return self.objects.get(key)

    def put(self, key: str, body: Any) -> None:
        self.objects[key] = json.loads(json.dumps(body))


def test_psi_is_zero_for_the_same_mix_and_large_for_a_different_one() -> None:
    week = {"a": 50, "b": 30, "c": 20}
    assert drift.psi(week, week) == 0.0
    assert drift.psi(week, {"a": 10, "b": 30, "c": 60}) > drift.PSI_ACT
    assert drift.psi({}, {}) == 0.0


def test_a_model_version_no_earlier_run_used_is_a_signal_to_act() -> None:
    since = NOW - drift.WEEK
    monitor = [run(2), run(9), run(16, model="gpt-5-mini-2026-02-01")]
    report, signals = drift.check_models(monitor, [calibration(10), calibration(16)], since)
    assert report["answer"]["new"] == ["gpt-5-mini-2026-02-01"]
    assert signals == [
        Signal("models", "act", "The answer model is now gpt-5-mini-2026-02-01, not seen before")
    ]


def test_the_first_week_of_runs_has_nothing_to_compare_with() -> None:
    _, signals = drift.check_models([run(16)], [calibration(16)], NOW - drift.WEEK)
    assert signals == []


def test_a_changed_source_marks_the_golden_cases_that_cite_it() -> None:
    latest = ingest(
        16, source("unsw-ai-legal-fact-sheet", added=2, removed=1), source("unsw-myit-ai")
    )
    report, signals = drift.check_sources([ingest(9), latest], date(2026, 11, 16), NOW)
    assert report["ingest"] == "202611" + "16-ingest"
    assert [c["source_id"] for c in report["changed"]] == ["unsw-ai-legal-fact-sheet"]
    cases = {c["case"] for c in report["cases_to_review"]}
    assert cases and all(
        "unsw-ai-legal-fact-sheet" in c["sources"] for c in report["cases_to_review"]
    )
    assert signals[0].level == "act" and "check these golden cases" in signals[0].summary


def test_a_missing_or_old_ingest_record_is_a_signal_and_partial_runs_don_t_count() -> None:
    _, none = drift.check_sources([], date(2026, 11, 16), NOW)
    assert none == [Signal("sources", "act", "No record of an ingest run")]
    _, old = drift.check_sources([ingest(2), ingest(16, partial=True)], date(2026, 11, 16), NOW)
    assert len(old) == 1 and "last ingest run on record" in old[0].summary


def test_a_quiet_week_has_no_source_signals() -> None:
    _, signals = drift.check_sources([ingest(16, source("unsw-myit-ai"))], date(2026, 11, 16), NOW)
    assert signals == []


def test_a_new_low_needs_enough_earlier_runs() -> None:
    assert not drift.new_low([0.8, 0.7])
    assert drift.new_low([0.84, 0.80, 0.82, 0.76])
    assert not drift.new_low([0.84, 0.80, 0.82, 0.81])
    monitor = [run(d, facts=f) for d, f in ((2, 0.84), (9, 0.80), (12, 0.82), (16, 0.70))]
    _, signals = drift.check_quality(monitor, [])
    assert signals == [Signal("quality", "watch", "facts 0.700: the full run's lowest yet")]


def mix(sources: dict[str, int], outcomes: dict[str, int]) -> drift.SpanQuery:
    def query(text: str, start: datetime, end: datetime) -> list[dict[str, str]]:
        assert "not ispresent(attributes.app.synthetic)" in text  # visitors only
        assert start >= drift.MIX_SINCE
        found = sources if "source_ids" in text else outcomes if "decline_reason" in text else {}
        return [{"category": k, "n": str(v)} for k, v in found.items()]

    return query


def test_the_mix_waits_for_enough_questions_then_sets_a_baseline_then_compares() -> None:
    store = FakeStore()
    few, _ = drift.check_mix(mix({"a": 3}, {"answered": 3}), store, NOW)
    assert few["status"].startswith("not compared: 3 visitors' questions")
    first, _ = drift.check_mix(mix({"a": 30, "b": 20}, {"answered": 45, "no_match": 5}), store, NOW)
    assert first["status"] == "baseline set: this week is the baseline"
    assert "drift/baseline.json" in store.objects
    same, quiet = drift.check_mix(
        mix({"a": 30, "b": 20}, {"answered": 45, "no_match": 5}), store, NOW
    )
    assert same["status"] == "compared" and quiet == []
    _, shifted = drift.check_mix(
        mix({"a": 5, "b": 45}, {"answered": 20, "no_match": 30}), store, NOW
    )
    assert {s.level for s in shifted} == {"act"}
    assert {s.summary.split(" shifted")[0] for s in shifted} == {
        "Questions' sources",
        "Questions' outcomes",
    }


def test_without_spans_the_mix_isn_t_checked() -> None:
    report, signals = drift.check_mix(None, FakeStore(), NOW)
    assert report["status"].startswith("not checked") and signals == []


def test_main_writes_the_report_and_counts_the_signals_to_act_on() -> None:
    store = FakeStore(
        runs_monitor=[run(2), run(9), run(16, model="gpt-5-mini-2026-02-01")],
        runs_calibration=[calibration(16)],
        ingest=[ingest(16, source("unsw-myit-ai"))],
    )
    emitted: list[int] = []
    assert drift.main([], store=store, query=mix({}, {}), metric=emitted.append, now=NOW) == 0
    latest = store.objects["drift/latest.json"]
    assert store.objects[f"drift/{latest['id']}.json"] == latest
    assert latest["act"] == 1 and emitted == [1]
    assert latest["mix"]["status"].startswith("not compared: 0")


class FakeS3Client:
    def __init__(self, objects: dict[str, Any]) -> None:
        self.objects = {k: json.dumps(v).encode() for k, v in objects.items()}

    def get_paginator(self, name: str) -> Any:
        client = self

        class Pages:
            def paginate(self, Bucket: str, Prefix: str) -> list[dict[str, Any]]:
                return [{"Contents": [{"Key": k} for k in client.objects if k.startswith(Prefix)]}]

        return Pages()

    def list_objects_v2(self, Bucket: str, Prefix: str, MaxKeys: int) -> dict[str, Any]:
        keys = sorted(k for k in self.objects if k.startswith(Prefix))[:MaxKeys]
        return {"Contents": [{"Key": k} for k in keys]} if keys else {}

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        if Key not in self.objects:
            # What S3 says to a reader that may list only some prefixes.
            raise RuntimeError("AccessDenied")
        return {"Body": io.BytesIO(self.objects[Key])}


def test_s3_records_skip_the_full_results() -> None:
    client = FakeS3Client(
        {
            "runs/monitor/a.json": {"id": "a"},
            "runs/monitor/results/a.json.gz": {},
            "runs/monitor/results/b.json": {"id": "results"},
        }
    )
    store = drift.S3Store("records", client)
    assert store.records("runs/monitor/") == [{"id": "a"}]
    # A missing object is None, though S3 would refuse the read itself.
    assert store.get("drift/baseline.json") is None
    assert store.get("runs/monitor/a.json") == {"id": "a"}


def test_a_logs_insights_query_waits_for_its_results(monkeypatch: pytest.MonkeyPatch) -> None:
    class Logs:
        def __init__(self) -> None:
            self.polls = 0

        def start_query(self, **kwargs: Any) -> dict[str, str]:
            assert kwargs["logGroupName"] == "aws/spans"
            return {"queryId": "q"}

        def get_query_results(self, queryId: str) -> dict[str, Any]:
            self.polls += 1
            if self.polls < 2:
                return {"status": "Running", "results": []}
            row = [{"field": "category", "value": "answered"}, {"field": "n", "value": "4"}]
            return {"status": "Complete", "results": [row]}

    monkeypatch.setattr(drift.time, "sleep", lambda seconds: None)
    rows = drift.insights_query(Logs())("q", NOW - timedelta(days=7), NOW)
    assert rows == [{"category": "answered", "n": "4"}]
    assert drift.counts(rows) == {"answered": 4}
