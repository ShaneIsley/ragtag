"""Run records: one small summary per evaluation run, so runs can be compared over time.

Each run has a kind, so runs that can't be compared stay apart:

- release: the candidate, run locally before `make release`
- post-deploy: production, straight after an apply
- monitor: production, on a schedule
- experiment: one setting changed, compared only with its own control run
- drill: a deliberately worse setting, to show an alarm firing
- calibration: the graders on fixed answers

Trends use production runs only: post-deploy and monitor (docs/decisions.md). Release
runs gate a release, but they measure a local candidate, so they stay off the trend.

A record names what was measured (the commit, a fingerprint of the files that decide
answers, and the prompt versions and models that answered) and what measured it (the
golden set, graders and thresholds), because a change to either moves scores.

Records go to evals/results/records/, and to S3 as well when EVAL_RECORDS_BUCKET is
set. `make release` checks them (`python -m evals.records release-check`): it ships
only code with a passing full release run.
"""

import gzip
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx

Kind = Literal["release", "post-deploy", "monitor", "experiment", "drill", "calibration"]
KINDS: tuple[Kind, ...] = (
    "release",
    "post-deploy",
    "monitor",
    "experiment",
    "drill",
    "calibration",
)
# Kinds whose runs make up the trend: production only. The others are compared only
# with their controls, or, for release runs, gate the release.
TREND_KINDS = frozenset({"post-deploy", "monitor"})

ROOT = Path(__file__).resolve().parents[1]
# On Lambda only /tmp is writable, so the scheduled runs set EVAL_RECORDS_DIR.
RECORDS_DIR = Path(os.environ.get("EVAL_RECORDS_DIR") or ROOT / "evals" / "results" / "records")
# The files that decide what the service answers. Two commits with the same
# fingerprint serve the same answers, so a docs-only commit needs no new release run.
ANSWER_PATHS = ("app", "prompts", "search/index.json", "Dockerfile.app")
# Dependencies count as the app image installs them (the main group only), so a
# change to dev or evaluation tools isn't a change to the answers.
DEPENDENCY_FILES = ("pyproject.toml", "uv.lock")
APP_DEPENDENCIES = (
    "uv",
    "export",
    "--frozen",
    "--no-default-groups",
    "--no-emit-project",
    "--no-header",
)
# What a run is measured with.
MEASURED_WITH = {
    "golden_set": ("evals/golden.yaml",),
    "graders": (
        "evals/assertions.py",
        "evals/scoring.py",
        "evals/facts.py",
        "evals/graders.py",
        "prompts/facts-check.v1.yaml",
    ),
    "thresholds": ("evals/thresholds.yaml",),
}


def _git(*args: str) -> str | None:
    try:
        done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return done.stdout


def commit() -> str | None:
    head = _git("rev-parse", "--short=12", "HEAD")
    return head.strip() if head else None


def fingerprint() -> str | None:
    """A hash of the committed answer files and the app's resolved dependencies, or
    None if any has uncommitted changes (or there's no git or uv, as on Lambda):
    then the run can't vouch for a release."""
    if _git("status", "--porcelain", "--", *ANSWER_PATHS, *DEPENDENCY_FILES) != "":
        return None
    tree = _git("ls-tree", "-r", "HEAD", "--", *ANSWER_PATHS)
    try:
        dependencies = subprocess.run(
            APP_DEPENDENCIES, cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    if not tree or not dependencies:
        return None
    return hashlib.sha256((tree + dependencies).encode()).hexdigest()[:12]


def file_hash(paths: Sequence[str]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update((ROOT / path).read_bytes())
    return digest.hexdigest()[:12]


def served_version(target: str, client: httpx.Client | None = None) -> str | None:
    """The release the target is running, from its health check."""
    try:
        with client or httpx.Client(timeout=10) as c:
            data = c.get(f"{target.rstrip('/')}/health").json()
    except (httpx.HTTPError, ValueError):
        return None
    version = data.get("version") if isinstance(data, Mapping) else None
    return str(version) if version else None


def default_kind(target: str, provider: str) -> Kind:
    """What a run by hand is, unless EVAL_KIND says otherwise: a comparison of models
    is an experiment; a run against this machine tests a candidate release."""
    if provider != "auto":
        return "experiment"
    host = httpx.URL(target).host
    return "release" if host in ("127.0.0.1", "localhost") else "post-deploy"


def answered_with(results: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    """How many cases each prompt version, model and provider answered."""
    counts: dict[str, Counter[str]] = {
        k: Counter() for k in ("answer_prompt", "rewrite_prompt", "model", "provider")
    }
    for row in results["results"]["results"]:
        response = ((row.get("response") or {}).get("metadata") or {}).get("response") or {}
        for key, field in (
            ("answer_prompt", "prompt_version"),
            ("rewrite_prompt", "rewrite_version"),
            ("model", "model"),
            ("provider", "provider"),
        ):
            if response.get(field):
                counts[key][str(response[field])] += 1
    return {k: dict(v) for k, v in counts.items()}


def build(
    results: Mapping[str, Any],
    summary: Mapping[str, Any],
    failures: Sequence[str],
    warnings: Sequence[str],
    *,
    kind: Kind,
    suite: str,
    target: str,
    provider: str,
    version: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    run = results["results"]
    started = str(run.get("timestamp") or datetime.now(UTC).isoformat())
    stats = run.get("stats") or {}
    record: dict[str, Any] = {
        "id": f"{started[:19].replace(':', '').replace('-', '')}Z-{kind}-{suite}",
        "kind": kind,
        "suite": suite,
        "started_at": started,
        "duration_s": round(stats.get("durationMs", 0) / 1000),
        # Never the address itself: records are published on the status page, and
        # the service's link is shared only with invited people.
        "target": "local" if default_kind(target, "auto") == "release" else "production",
        "provider": provider,
        "measured": {
            "commit": commit(),
            "fingerprint": fingerprint(),
            "version": version,
            **answered_with(results),
        },
        "measured_with": {name: file_hash(paths) for name, paths in MEASURED_WITH.items()},
        "passed": not failures,
        "failures": list(failures),
        "warnings": list(warnings),
        "summary": dict(summary),
        "tokens": {
            "input": (stats.get("tokenUsage") or {}).get("prompt", 0),
            "output": (stats.get("tokenUsage") or {}).get("completion", 0),
        },
    }
    if note:
        record["note"] = note
    return record


def save(record: Mapping[str, Any], directory: Path | None = None) -> Path:
    directory = directory or RECORDS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    # The same results judged again (after a change to the thresholds, say) get a new
    # record, so the first judgement stays on file.
    path = directory / f"{record['id']}.json"
    judged = 1
    while path.exists():
        judged += 1
        record = {**record, "id": f"{record['id'].split('~')[0]}~{judged}"}
        path = directory / f"{record['id']}.json"
    path.write_text(json.dumps(record, indent=2) + "\n")
    bucket = os.environ.get("EVAL_RECORDS_BUCKET")
    if bucket:
        upload(record, bucket)
    return path


def keep_results(results_file: Path, record_path: Path) -> Path:
    """Keeps the run's full results beside its record, compressed, so each case's
    answer and verdicts can be read later (latest.json is overwritten by the next run)."""
    kept = record_path.parent / "results" / f"{record_path.stem}.json.gz"
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_bytes(gzip.compress(results_file.read_bytes()))
    bucket = os.environ.get("EVAL_RECORDS_BUCKET")
    if bucket:
        record = json.loads(record_path.read_text())
        _put(
            bucket,
            f"runs/{record['kind']}/results/{kept.name}",
            kept.read_bytes(),
            "application/gzip",
        )
    return kept


def upload(record: Mapping[str, Any], bucket: str) -> None:
    body = json.dumps(record, indent=2).encode()
    _put(bucket, f"runs/{record['kind']}/{record['id']}.json", body, "application/json")


def _put(bucket: str, key: str, body: bytes, content_type: str) -> None:
    import boto3

    boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)


def load_all(directory: Path = RECORDS_DIR) -> list[dict[str, Any]]:
    if not directory.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(directory.glob("*.json"))]


def release_check(records: Sequence[Mapping[str, Any]], current: str | None) -> str | None:
    """Why the current code can't be released, or None if a passing full release run
    vouches for it."""
    if current is None:
        return "The answer files have uncommitted changes, so no run can vouch for them."
    for record in records:
        measured = record.get("measured") or {}
        if (
            record.get("kind") == "release"
            and record.get("suite") == "full"
            and record.get("passed")
            and measured.get("fingerprint") == current
        ):
            return None
    return (
        f"No passing full release run for these answer files (fingerprint {current}). "
        "Run the candidate locally (make up), then make eval."
    )


def record_override(reason: str, directory: Path = RECORDS_DIR) -> None:
    """Notes a release made without a passing release run, and why."""
    directory.mkdir(parents=True, exist_ok=True)
    entry = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": commit(),
        "fingerprint": fingerprint(),
        "reason": reason,
    }
    with (directory / "overrides.jsonl").open("a") as f:
        f.write(json.dumps(entry) + "\n")


def main(argv: Sequence[str]) -> int:
    if argv == ["release-check"]:
        reason = release_check(load_all(), fingerprint())
        if reason:
            print(reason, file=sys.stderr)
            return 1
        print(f"A passing release run vouches for these answer files ({fingerprint()}).")
        return 0
    if len(argv) == 2 and argv[0] == "release-override":
        record_override(argv[1])
        print(f"Releasing without a release run, recorded: {argv[1]}", file=sys.stderr)
        return 0
    if argv == ["fingerprint"]:
        print(fingerprint() or "uncommitted")
        return 0
    print(
        'Usage: python -m evals.records release-check | release-override "why" | fingerprint',
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
