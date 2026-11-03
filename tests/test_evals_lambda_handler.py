import io
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.settings import Settings
from evals import calibration, drift, gate, lambda_handler, status


@pytest.fixture
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {"gate": 0}
    results = tmp_path / "latest.json"
    monkeypatch.setattr(lambda_handler, "RESULTS", results)
    monkeypatch.setattr(
        lambda_handler,
        "get_settings",
        lambda: Settings(_env_file=None, azure_openai_api_key="key"),
    )

    def promptfoo(args: list[str], **_: Any) -> None:
        seen["promptfoo"] = args
        seen["env"] = {k: os.environ.get(k) for k in ("EVAL_SUITE", "EVAL_PROVIDER", "EVAL_KIND")}
        if seen.get("writes", True):
            results.write_text(json.dumps({"results": {"results": []}}))

    def judged(argv: list[str]) -> int:
        seen["gate_argv"] = argv
        return int(seen["gate"])

    monkeypatch.setattr(subprocess, "run", promptfoo)
    monkeypatch.setattr(gate, "main", judged)
    seen["status"] = 0

    def publish() -> None:
        seen["status"] += 1
        if seen.get("status_fails"):
            raise RuntimeError("AccessDenied")

    monkeypatch.setattr(status, "publish", publish)
    monkeypatch.delenv("EVAL_RECORDS_BUCKET", raising=False)
    # The handler sets these for promptfoo; setenv first, so monkeypatch removes them
    # again afterwards whatever the handler wrote.
    for key in ("EVAL_SUITE", "EVAL_PROVIDER", "EVAL_KIND", "AZURE_OPENAI_API_KEY"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    return seen


def test_a_scheduled_run_asks_its_suite_and_records_a_monitor_run(run: dict[str, Any]) -> None:
    assert lambda_handler.handler({"suite": "canary"}, None) == {"suite": "canary", "passed": True}
    assert run["env"] == {"EVAL_SUITE": "canary", "EVAL_PROVIDER": "auto", "EVAL_KIND": "monitor"}
    assert run["gate_argv"][1:] == ["--kind", "monitor", "--suite", "canary"]
    assert "--no-cache" in run["promptfoo"]


def test_a_deploy_s_check_is_recorded_as_a_post_deploy_run(run: dict[str, Any]) -> None:
    lambda_handler.handler({"suite": "canary", "kind": "post-deploy"}, None)
    assert run["env"]["EVAL_KIND"] == "post-deploy"
    assert run["gate_argv"][1:] == ["--kind", "post-deploy", "--suite", "canary"]
    with pytest.raises(ValueError, match="kind must be one of"):
        lambda_handler.handler({"suite": "canary", "kind": "release"}, None)


def test_the_standby_run_asks_the_failover_model(run: dict[str, Any]) -> None:
    lambda_handler.handler({"suite": "standby"}, None)
    assert run["env"]["EVAL_PROVIDER"] == "bedrock"


def test_a_run_that_fails_the_gate_raises_so_the_alarm_fires(run: dict[str, Any]) -> None:
    run["gate"] = 1
    with pytest.raises(lambda_handler.GateFailed, match="full run failed the gate"):
        lambda_handler.handler({"suite": "full"}, None)


def test_a_run_without_results_raises(run: dict[str, Any]) -> None:
    run["writes"] = False
    with pytest.raises(lambda_handler.GateFailed, match="no results"):
        lambda_handler.handler({"suite": "canary"}, None)


def test_calibration_runs_the_judges_without_promptfoo(
    run: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def calibrate(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    monkeypatch.setattr(calibration, "main", calibrate)
    assert lambda_handler.handler({"suite": "calibration"}, None) == {
        "suite": "calibration",
        "passed": True,
    }
    assert calls == [[]]
    assert "promptfoo" not in run


def test_a_failed_calibration_raises_so_the_alarm_fires(
    run: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(calibration, "main", lambda argv: 1)
    with pytest.raises(lambda_handler.GateFailed, match="calibration failed"):
        lambda_handler.handler({"suite": "calibration"}, None)


def test_drift_runs_without_the_lock(
    run: dict[str, Any], bucket: "FakeS3", monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []
    bucket.objects[lambda_handler.LOCK_KEY] = lock(time.time())  # a canary is running

    def check(argv: list[str]) -> int:
        seen.append(argv == [])
        return 0

    monkeypatch.setattr(drift, "main", check)
    assert lambda_handler.handler({"suite": "drift"}, None) == {"suite": "drift", "passed": True}
    assert seen == [True]
    assert lambda_handler.LOCK_KEY in bucket.objects  # the other run's lock, untouched


def test_every_run_rebuilds_the_status_snapshot_even_when_it_fails(run: dict[str, Any]) -> None:
    lambda_handler.handler({"suite": "canary"}, None)
    run["gate"] = 1
    with pytest.raises(lambda_handler.GateFailed):
        lambda_handler.handler({"suite": "full"}, None)
    assert run["status"] == 2


def test_a_failed_snapshot_doesn_t_hide_the_run_s_result(run: dict[str, Any]) -> None:
    run["status_fails"] = True
    assert lambda_handler.handler({"suite": "canary"}, None)["passed"] is True


def test_the_status_suite_only_rebuilds_the_snapshot(run: dict[str, Any]) -> None:
    assert lambda_handler.handler({"suite": "status"}, None) == {"suite": "status", "passed": True}
    assert run["status"] == 1 and "promptfoo" not in run


def test_an_unknown_suite_is_refused(run: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="suite must be one of"):
        lambda_handler.handler({"suite": "nightly"}, None)


class FakeS3:
    """Just enough of S3 for the lock, including put-if-absent."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(
        self, Bucket: str, Key: str, Body: bytes, IfNoneMatch: str | None = None
    ) -> None:
        if IfNoneMatch == "*" and Key in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[Key] = Body

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, Bucket: str, Key: str) -> None:
        self.objects.pop(Key, None)


@pytest.fixture
def bucket(run: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> FakeS3:
    fake = FakeS3()
    monkeypatch.setenv("EVAL_RECORDS_BUCKET", "records")
    monkeypatch.setattr(lambda_handler, "s3", lambda: fake)
    original = subprocess.run

    def promptfoo_sees_the_lock(args: list[str], **kwargs: Any) -> None:
        run["locked_during_run"] = lambda_handler.LOCK_KEY in fake.objects
        original(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", promptfoo_sees_the_lock)
    return fake


def lock(started: float) -> bytes:
    return json.dumps({"suite": "full", "started": started}).encode()


def test_a_run_holds_the_lock_while_it_runs_and_then_releases_it(
    run: dict[str, Any], bucket: FakeS3
) -> None:
    assert lambda_handler.handler({"suite": "canary"}, None)["passed"] is True
    assert run["locked_during_run"] is True
    assert bucket.objects == {}


def test_a_run_skips_while_another_holds_the_lock(run: dict[str, Any], bucket: FakeS3) -> None:
    held = lock(time.time() - 60)
    bucket.objects[lambda_handler.LOCK_KEY] = held
    result = lambda_handler.handler({"suite": "canary"}, None)
    assert result == {"suite": "canary", "skipped": "another run is in progress"}
    assert "promptfoo" not in run
    assert bucket.objects[lambda_handler.LOCK_KEY] == held  # the other run's lock, untouched


def test_a_lock_left_by_a_run_that_died_is_taken_over(run: dict[str, Any], bucket: FakeS3) -> None:
    bucket.objects[lambda_handler.LOCK_KEY] = lock(time.time() - lambda_handler.LOCK_STALE_S - 1)
    assert lambda_handler.handler({"suite": "canary"}, None)["passed"] is True
    assert bucket.objects == {}


def test_a_failed_run_still_releases_the_lock(run: dict[str, Any], bucket: FakeS3) -> None:
    run["gate"] = 1
    with pytest.raises(lambda_handler.GateFailed):
        lambda_handler.handler({"suite": "canary"}, None)
    assert bucket.objects == {}
