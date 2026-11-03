import json
from datetime import UTC, datetime, timedelta
from typing import Any

from evals import drift, records, status

NOW = datetime(2026, 11, 16, 18, 0, tzinfo=UTC)
GOLDEN = records.file_hash(("evals/golden.yaml",))


def run(
    day: int, suite: str = "full", version: str = "aaaaaaa1111", passed: bool = True, **means: float
) -> dict[str, Any]:
    return {
        "id": f"202611{day:02d}-{suite}",
        "suite": suite,
        "started_at": datetime(2026, 11, day, 17, tzinfo=UTC).isoformat(),
        "passed": passed,
        "warnings": [],
        "failures": [],
        "measured": {"version": version, "model": {"gpt-5-mini-2025-08-07": 30}},
        "measured_with": {"golden_set": GOLDEN},
        "summary": {
            "tests": 40,
            "means": means,
            "latency_p90_s": 7.2,
            "failed_checks": [["injection-poem", "forbidden_text"], ["what-is-scout", "facts"]],
        },
    }


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


def usage_query(answered: int, declined: int, masked: int) -> drift.SpanQuery:
    def query(text: str, start: datetime, end: datetime) -> list[dict[str, str]]:
        assert "not ispresent(attributes.app.synthetic)" in text
        if "category" in text:
            return [
                {"category": "answered", "n": str(answered)},
                {"category": "no_match", "n": str(declined)},
            ]
        if "app.masked" in text:
            return [{"n": str(masked)}]
        return [{"n": "0"}]

    return query


def snapshot(**query: int) -> dict[str, Any]:
    store = FakeStore(
        runs_monitor=[
            run(2, facts=0.84, groundedness=1.0),
            run(9, version="bbbbbbb2222", facts=0.80, groundedness=0.99),
            run(16, version="bbbbbbb2222", facts=0.83, groundedness=0.99),
            run(16, suite="canary"),
            run(15, suite="canary", passed=False),
            run(16, suite="standby", facts=0.5, groundedness=0.88),
        ],
        runs_calibration=[
            {
                "id": "c",
                "started_at": datetime(2026, 11, 16, 15, tzinfo=UTC).isoformat(),
                "passed": True,
                "measured": {"judge_model": {"gpt-5-mini-2025-08-07": 15}},
                "summary": {"agreement": {"facts": 1.0, "groundedness": 0.933, "relevance": 1.0}},
            }
        ],
    )
    store.objects["drift/latest.json"] = {
        "generated_at": NOW.isoformat(),
        "signals": [{"check": "sources", "level": "act", "summary": "Sources changed"}],
        "sources": {
            "cases_to_review": [{"case": "what-is-scout", "sources": ["unsw-teaching-scout"]}]
        },
        "models": {"answer": {"this_week": ["gpt-5-mini-2025-08-07"]}},
        "mix": {"status": "not compared: 3 visitors' questions, fewer than 20"},
    }
    answered, declined, masked = (
        query.get("answered", 40),
        query.get("declined", 9),
        query.get("masked", 3),
    )
    return status.build(store, usage_query(answered, declined, masked), NOW)


def test_small_counts_never_reach_the_snapshot() -> None:
    assert status.shown(0) == 0 and status.shown(4) == "fewer than 5" and status.shown(5) == 5
    week = snapshot(answered=3, declined=1, masked=2)["usage"]["days_7"]
    assert week["questions"] == "fewer than 5"
    assert week["answered"] == "fewer than 5" and week["masked"] == "fewer than 5"
    assert week["failovers"] == 0


def test_usage_counts_visitors_only_from_when_they_could_be_told_apart() -> None:
    usage = snapshot()["usage"]
    assert usage["days_30"]["questions"] == 49 and usage["days_30"]["declined"] == 9
    assert usage["days_30"]["from"] == "2 Nov"  # drift.MIX_SINCE, in Sydney


def test_a_window_that_hasn_t_started_counts_nothing_and_asks_nothing() -> None:
    def refuse(text: str, start: datetime, end: datetime) -> list[dict[str, str]]:
        raise AssertionError("queried a window that ends before it starts")

    before = drift.MIX_SINCE - timedelta(hours=1)
    week = status.usage(refuse, before)
    assert week is not None and week["days_7"]["questions"] == 0
    report, _ = drift.check_mix(refuse, FakeStore(), before)
    assert report["questions"] == 0


def test_quality_shows_the_runs_in_order_with_releases_marked() -> None:
    q = snapshot()["quality"]
    assert [r["day"] for r in q["full_runs"]] == ["3 Nov", "10 Nov", "17 Nov"]  # Sydney dates
    assert q["latest_full"]["means"]["facts"] == 0.83
    assert q["markers"] == [{"day": "10 Nov", "what": "release bbbbbbb"}]
    assert [n["passed"] for n in q["canary"]] == [False, True]
    assert {m["name"] for m in q["measures"]} == set(status.MEASURES)


def test_the_assurance_framework_s_seven_parts_each_have_a_live_signal() -> None:
    parts = snapshot()["assurance"]
    assert [p["part"] for p in parts] == [
        "Business value",
        "Sensitive data",
        "Harm",
        "Fairness",
        "Transparency and accountability",
        "Resilience and safety",
        "Compliance",
    ]
    by = {p["part"]: p for p in parts}
    assert by["Business value"]["signal"].startswith("Visitors' questions answered")
    assert by["Business value"]["signal"].endswith(": 40.")
    assert "6 of 7 prompt-injection cases resisted" in by["Harm"]["also"]
    # The fixture's two failed cases are both students' questions.
    assert "students 8 of 10" in by["Fairness"]["signal"]
    assert "groundedness 93%" in by["Resilience and safety"]["also"]


def test_case_counts_need_a_run_of_today_s_golden_set() -> None:
    old = run(16, facts=0.8)
    old["measured_with"]["golden_set"] = "something-else"
    assert status.injections(old) is None and status.by_audience(old) == {}


def test_drift_and_known_issues_come_through() -> None:
    snap = snapshot()
    assert snap["drift"]["signals"][0]["summary"] == "Sources changed"
    assert snap["drift"]["cases_to_review"][0]["case"] == "what-is-scout"
    assert snap["known_issues"] and all("why" in k for k in snap["known_issues"])
    assert not any(k["why"].endswith(")") for k in snap["known_issues"])  # no run notes
    assert list(status.by_audience(run(16, facts=0.8))) == [
        "staff",
        "students",
        "researchers",
        "questions for anyone",
    ]
    assert snap["standby"]["means"]["facts"] == 0.5


def test_publish_writes_the_snapshot_to_the_bucket(monkeypatch: Any) -> None:
    monkeypatch.setenv("EVAL_RECORDS_BUCKET", "records")
    store = FakeStore()
    status.publish(store, usage_query(0, 0, 0))
    assert set(store.objects) == {status.SNAPSHOT_KEY}


def record(kind: str, version: str, at: str, passed: bool = True, **extra: Any) -> dict[str, Any]:
    """A run record with only what the releases read."""
    return {
        "kind": kind,
        "suite": extra.pop("suite", "full"),
        "started_at": at,
        "duration_s": 240,
        "passed": passed,
        "measured": {"version": version, "fingerprint": extra.pop("fingerprint", "a")},
        "summary": {"tests": 40},
        **extra,
    }


def test_releases_show_the_gate_then_production_newest_first() -> None:
    # 3 Nov 2026, as it happened: one stopped, one passed on a second try, one with no gate.
    stopped = "rule failed: assurance-framework-parts facts: 1 of 2 facts stated"
    flaky = "rule failed: other-university-policy outcome: expected decline, answered"
    gate = [
        record("release", "9eca6e031df2", "2026-11-02T10:21:33Z"),
        record("release", "1e54905616a0", "2026-11-03T01:21:28Z", False, failures=[stopped]),
        record(
            "release",
            "ab24bc333370",
            "2026-11-03T04:44:12Z",
            False,
            failures=[flaky],
            fingerprint="b",
        ),
        record(
            "release", "ab24bc333370", "2026-11-03T05:17:40Z", fingerprint="b", note="The review"
        ),
    ]
    production = [
        record("post-deploy", "9eca6e031df2", "2026-11-02T10:38:17Z", suite="canary"),
        record(
            "post-deploy", "ab24bc333370", "2026-11-03T05:35:31Z", suite="canary", fingerprint="b"
        ),
        record("monitor", "467f3ff8d35e", "2026-11-03T05:59:36Z", fingerprint="b"),
    ]
    rows = status.releases(gate, production)
    assert [r["version"] for r in rows] == ["467f3ff", "ab24bc3", "1e54905", "9eca6e0"]
    newest, reviewed, stopped_row, first = rows
    assert newest["gate"] == [] and newest["released"]
    assert newest["after"] == {"passed": True, "what": "Full check", "time": "5:03 pm"}
    assert newest["answer_path_changed"] is False
    assert [g["passed"] for g in reviewed["gate"]] == [False, True]
    assert reviewed["gate"][0]["failure"] == "other-university-policy (outcome)"
    assert reviewed["note"] == "The review" and reviewed["answer_path_changed"] is True
    assert reviewed["after"]["what"] == "Post-deploy check" and reviewed["when"] == "3 Nov, 4:39 pm"
    assert not stopped_row["released"] and stopped_row["after"] is None
    assert stopped_row["gate"][0]["failure"] == "assurance-framework-parts (facts)"
    assert first["answer_path_changed"] is None  # nothing older to compare with
