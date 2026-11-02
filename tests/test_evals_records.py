import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from evals import records


def promptfoo_results(*responses: dict[str, Any]) -> dict[str, Any]:
    return {
        "results": {
            "timestamp": "2026-11-02T03:00:05.123Z",
            "stats": {"durationMs": 180_400, "tokenUsage": {"prompt": 900, "completion": 120}},
            "results": [{"response": {"metadata": {"response": r}}} for r in responses],
        }
    }


RESPONSE = {
    "prompt_version": "answer.v3",
    "rewrite_version": "rewrite.v2",
    "model": "gpt-5-mini-2025-08-07",
    "provider": "azure",
}


def build(**overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "kind": "monitor",
        "suite": "canary",
        "target": "https://d111.cloudfront.net",
        "provider": "auto",
        "version": "7bf129a927dd",
    }
    arguments.update(overrides)
    return records.build(
        promptfoo_results(RESPONSE, {**RESPONSE, "provider": "bedrock", "model": "haiku"}, {}),
        {"tests": 3},
        [],
        ["known issue: deepseek-restricted facts"],
        **arguments,
    )


def test_a_record_names_what_was_measured_and_what_measured_it() -> None:
    record = build()
    assert record["id"] == "20261102T030005Z-monitor-canary"
    assert record["passed"] is True and record["duration_s"] == 180
    measured = record["measured"]
    assert measured["version"] == "7bf129a927dd"
    assert measured["answer_prompt"] == {"answer.v3": 2}
    assert measured["provider"] == {"azure": 1, "bedrock": 1}
    assert measured["model"] == {"gpt-5-mini-2025-08-07": 1, "haiku": 1}
    assert set(record["measured_with"]) == {"golden_set", "graders", "thresholds"}
    assert record["tokens"] == {"input": 900, "output": 120}


def test_the_target_is_recorded_as_local_or_production_never_its_address() -> None:
    assert build(target="http://127.0.0.1:8001", kind="release")["target"] == "local"
    record = build()
    assert record["target"] == "production"
    assert "cloudfront" not in json.dumps(record)


@pytest.mark.parametrize(
    ("target", "provider", "kind"),
    [
        ("http://127.0.0.1:8001", "auto", "release"),
        ("http://localhost:8001", "auto", "release"),
        ("https://d111.cloudfront.net", "auto", "post-deploy"),
        ("https://d111.cloudfront.net", "bedrock", "experiment"),
        ("http://127.0.0.1:8001", "azure", "experiment"),
    ],
)
def test_a_run_by_hand_gets_a_kind_from_its_target_and_provider(
    target: str, provider: str, kind: str
) -> None:
    assert records.default_kind(target, provider) == kind


def test_the_served_version_comes_from_the_health_check() -> None:
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"status": "ok", "version": "abc123"})
    )
    assert (
        records.served_version("https://x.example", httpx.Client(transport=transport)) == "abc123"
    )
    down = httpx.MockTransport(lambda r: httpx.Response(503, text="unavailable"))
    assert records.served_version("https://x.example", httpx.Client(transport=down)) is None


def release(fingerprint: str, *, passed: bool = True, suite: str = "full", kind: str = "release"):
    return {
        "kind": kind,
        "suite": suite,
        "passed": passed,
        "measured": {"fingerprint": fingerprint},
    }


def test_a_passing_full_release_run_vouches_for_the_same_answer_files() -> None:
    assert records.release_check([release("f1")], "f1") is None


@pytest.mark.parametrize(
    "run",
    [
        release("f1", passed=False),
        release("f1", suite="canary"),
        release("f1", kind="post-deploy"),
        release("f0"),
    ],
)
def test_nothing_else_vouches_for_a_release(run: dict[str, Any]) -> None:
    reason = records.release_check([run], "f1")
    assert reason and "fingerprint f1" in reason


def test_uncommitted_answer_files_cant_be_released() -> None:
    reason = records.release_check([release("f1")], None)
    assert reason and "uncommitted" in reason


def test_records_are_saved_one_file_each(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVAL_RECORDS_BUCKET", raising=False)
    path = records.save(build(), tmp_path)
    assert path.name == "20261102T030005Z-monitor-canary.json"
    assert records.load_all(tmp_path) == [json.loads(path.read_text())]


def test_an_override_is_noted_with_its_reason(tmp_path: Path) -> None:
    records.record_override("Hotfix: the index was rebuilt", tmp_path)
    entry = json.loads((tmp_path / "overrides.jsonl").read_text())
    assert entry["reason"] == "Hotfix: the index was rebuilt"
    assert records.load_all(tmp_path) == []


def test_judging_the_same_results_again_keeps_the_first_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EVAL_RECORDS_BUCKET", raising=False)
    first = records.save({**build(), "passed": False}, tmp_path)
    second = records.save(build(), tmp_path)
    assert first.name == "20261102T030005Z-monitor-canary.json"
    assert second.name == "20261102T030005Z-monitor-canary~2.json"
    assert [r["passed"] for r in records.load_all(tmp_path)] == [False, True]
    assert json.loads(second.read_text())["id"] == "20261102T030005Z-monitor-canary~2"


def test_a_runs_full_results_are_kept_beside_its_record(tmp_path: Path) -> None:
    import gzip

    results = tmp_path / "latest.json"
    results.write_text('{"results": {}}')
    kept = records.keep_results(
        results, tmp_path / "records" / "20261102T030005Z-monitor-canary.json"
    )
    assert kept == tmp_path / "records" / "results" / "20261102T030005Z-monitor-canary.json.gz"
    assert gzip.decompress(kept.read_bytes()) == b'{"results": {}}'
