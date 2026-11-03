"""The scheduled evaluation on AWS Lambda: one suite of the golden set against
production, judged by the gate and recorded in S3 as a monitor run.

EventBridge Scheduler invokes it with {"suite": "calibration"} and {"suite": "canary"}
nightly, and {"suite": "full"}, {"suite": "standby"} and {"suite": "drift"} weekly after
the ingest (infra/eval.tf). The standby suite asks the failover model. Calibration asks
no app: the judges score the fixed answers in evals/calibration.yaml. Drift calls no
model: it reads the records and spans (evals/drift.py). A run that fails raises, so
Lambda counts an error and the eval_failed alarm emails. After every run, passed or
not, the status page's snapshot is rebuilt (evals/status.py); {"suite": "status"}
rebuilds it alone. A deploy adds "kind": "post-deploy", so its check is recorded as
one; scheduled runs are monitor runs.

Only one run that calls the models goes at a time: runs share the chat deployment's rate limit, and
overlapping runs throttle each other into failures. A run takes a lock object in the
records bucket, written only if it doesn't exist; a run that finds it held exits
without asking anything. Lambda can't cap the function at one instance itself, because
this account's concurrency quota is too small to reserve any.

The function's environment names the target (EVAL_TARGET_URL), the records bucket
(EVAL_RECORDS_BUCKET) and the Azure OpenAI key's secret, which the graders need.
"""

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

from app.settings import get_settings
from evals import calibration, drift, gate, status
from evals.suites import SUITES

PROMPTFOO = os.environ.get("PROMPTFOO_BIN", "/opt/promptfoo/node_modules/.bin/promptfoo")
CONFIG = Path(__file__).with_name("promptfooconfig.yaml")
RESULTS = Path("/tmp/latest.json")
PROVIDERS = {"standby": "bedrock"}
KINDS = ("monitor", "post-deploy")
CALIBRATION = "calibration"
DRIFT = "drift"
STATUS = "status"
OTHER_RUNS = (CALIBRATION, DRIFT, STATUS)
# Leaves a minute of Lambda's 15 for the gate and the record.
PROMPTFOO_TIMEOUT_S = 840
LOCK_KEY = "lock/eval-running.json"
# A lock older than Lambda's limit belongs to a run that died without releasing it.
LOCK_STALE_S = 16 * 60
# A deploy's check waits this long for another run to finish, then fails loudly: skipped,
# the release would go unchecked (3 Nov 2026, behind a full run started a minute earlier).
# A canary takes about 4 minutes, so this leaves it room within Lambda's 15.
LOCK_WAIT_S = 9 * 60
LOCK_POLL_S = 30


class GateFailed(Exception):
    """The run failed the gate; its record says why."""


def s3() -> Any:
    return boto3.client("s3")


def take_lock(client: Any, bucket: str, suite: str) -> bool:
    """True if this run now holds the lock; False if another run holds it."""
    body = json.dumps({"suite": suite, "started": time.time()}).encode()
    try:
        client.put_object(Bucket=bucket, Key=LOCK_KEY, Body=body, IfNoneMatch="*")
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("PreconditionFailed", "ConditionalRequestConflict"):
            raise
    held = json.loads(client.get_object(Bucket=bucket, Key=LOCK_KEY)["Body"].read())
    if time.time() - float(held["started"]) < LOCK_STALE_S:
        print(f"Skipped: a {held['suite']} run is already in progress.")
        return False
    client.put_object(Bucket=bucket, Key=LOCK_KEY, Body=body)
    return True


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    suite = event.get("suite", "canary")
    kind = event.get("kind", "monitor")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}, not {kind!r}")
    if suite not in (*SUITES, *OTHER_RUNS):
        names = ", ".join((*SUITES, *OTHER_RUNS))
        raise ValueError(f"suite must be one of {names}, not {suite!r}")
    if suite == STATUS:
        status.publish()
        return {"suite": STATUS, "passed": True}
    if suite == DRIFT:
        # No model calls, so no share of the rate limit to wait for.
        try:
            drift.main([])
        finally:
            refresh_status()
        return {"suite": DRIFT, "passed": True}
    bucket = os.environ.get("EVAL_RECORDS_BUCKET")
    client = s3() if bucket else None
    if client and bucket and not take_lock(client, bucket, suite):
        if kind != "post-deploy":
            return {"suite": suite, "skipped": "another run is in progress"}
        waited = 0
        while not take_lock(client, bucket, suite):
            if waited >= LOCK_WAIT_S:
                raise GateFailed(
                    f"The post-deploy check couldn't start: another run held the lock for "
                    f"{LOCK_WAIT_S // 60} minutes."
                )
            time.sleep(LOCK_POLL_S)
            waited += LOCK_POLL_S
    try:
        return calibrate() if suite == CALIBRATION else run(suite, kind, event.get("note"))
    finally:
        if client and bucket:
            client.delete_object(Bucket=bucket, Key=LOCK_KEY)
        refresh_status()


def refresh_status() -> None:
    """Rebuilds the status page's snapshot. A failure here mustn't hide the run's own
    result, so it's only printed; the page keeps showing the previous snapshot."""
    try:
        status.publish()
    except Exception as e:
        print(f"Couldn't rebuild the status snapshot: {type(e).__name__}: {e}")


def calibrate() -> dict[str, Any]:
    """The judges on the calibration set, in this process: there's no app to ask."""
    if calibration.main([]) != 0:
        raise GateFailed("Judge calibration failed: see this log and the run's record.")
    return {"suite": CALIBRATION, "passed": True}


def run(suite: str, kind: str = "monitor", note: str | None = None) -> dict[str, Any]:
    os.environ.update(
        {
            "EVAL_SUITE": suite,
            "EVAL_PROVIDER": PROVIDERS.get(suite, "auto"),
            "EVAL_KIND": kind,
            # Read once here, so promptfoo's Python workers don't each fetch it.
            "AZURE_OPENAI_API_KEY": get_settings().azure_openai_api_key.get_secret_value(),
        }
    )
    RESULTS.unlink(missing_ok=True)
    # promptfoo's exit code is ignored, as in make eval: the gate decides.
    subprocess.run(
        [PROMPTFOO, "eval", "-c", str(CONFIG), "--no-cache", "-o", str(RESULTS)],
        check=False,
        timeout=PROMPTFOO_TIMEOUT_S,
    )
    if not RESULTS.exists():
        raise GateFailed(f"promptfoo wrote no results for the {suite} run")
    # A deploy's check carries the release's note, for the status page's releases.
    argv = [str(RESULTS), "--kind", kind, "--suite", suite, *(["--note", note] if note else [])]
    if gate.main(argv) != 0:
        raise GateFailed(f"The {suite} run failed the gate: see this log and the run's record.")
    return {"suite": suite, "passed": True}
