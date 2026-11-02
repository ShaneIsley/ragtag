"""Drift checks: has anything the answers depend on changed without a release?

Weekly, after the ingest and the full run (evals/lambda_handler.py), this reads the run
records, the ingest's record of what it changed, and the app's spans, and reports:

- models: an answer, standby or judge model version not seen in earlier runs
- sources: what the latest ingest changed, and the golden cases citing those sources,
  which need checking against the new text; and cases past their review date
- quality: a measure in the latest full run, or a judge's calibration agreement, at a
  new low
- question mix: what visitors' questions retrieve, how they end, and how well they
  match, this week against a baseline week, as a population stability index (PSI).
  The evaluation's own requests are left out (app.synthetic)

Each finding is a signal: "act" (someone should look) or "watch". The report goes to
drift/latest.json in the records bucket, for the status page; the count of "act"
signals goes to CloudWatch, where the drift alarm emails. Locally (`make drift`), it
reads the local records only and skips the spans, which are only in AWS.
"""

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

from evals import records
from evals.golden import load

Level = Literal["act", "watch"]
WEEK = timedelta(days=7)
# Visitors' questions are told apart from the evaluation's from release b74f9e9 on,
# deployed about 03:25 UTC on 2 November 2026; spans before it are left out.
MIX_SINCE = datetime(2026, 11, 2, 4, 0, tzinfo=UTC)
# Fewer questions than this in a week, and the mix isn't compared: the PSI of a handful
# of questions is noise.
MIX_MINIMUM = 20
# The usual reading of a PSI: under 0.1 stable, 0.1 to 0.25 a moderate shift, over 0.25
# a large one.
PSI_WATCH, PSI_ACT = 0.1, 0.25
# A measure is at a new low only against at least this many earlier runs.
LOW_AFTER = 3
LOG_GROUP = "aws/spans"
NAMESPACE, METRIC = "PolicyDesk", "DriftSignals"
LOCAL_DIR = Path(__file__).resolve().parents[1] / "evals" / "results" / "drift"

MIX_QUERIES = {
    "sources": """
        filter name = "search" and not ispresent(attributes.app.synthetic)
        | fields jsonParse(@message) as span
        | unnest span.attributes.`app.source_ids` into category
        | stats count(*) as n by category""",
    "outcomes": """
        filter name = "answer" and not ispresent(attributes.app.synthetic)
        | fields coalesce(attributes.app.decline_reason, "answered") as category
        | stats count(*) as n by category""",
    "similarity": """
        filter name = "search" and not ispresent(attributes.app.synthetic)
        | fields floor(attributes.app.top_similarity * 10) / 10 as category
        | stats count(*) as n by category""",
}


@dataclass(frozen=True)
class Signal:
    check: str
    level: Level
    summary: str


class Store(Protocol):
    def records(self, prefix: str) -> list[dict[str, Any]]: ...
    def get(self, key: str) -> dict[str, Any] | None: ...
    def put(self, key: str, body: Mapping[str, Any]) -> None: ...


class S3Store:
    """The records bucket: runs/<kind>/, ingest/ and drift/."""

    def __init__(self, bucket: str, client: Any = None) -> None:
        import boto3

        self.bucket, self.client = bucket, client or boto3.client("s3")

    def records(self, prefix: str) -> list[dict[str, Any]]:
        found = []
        pages = self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=prefix
        )
        for page in pages:
            for item in page.get("Contents", []):
                key = item["Key"]
                if key.endswith(".json") and "/results/" not in key:
                    found.append(self._read(key))
        return found

    def get(self, key: str) -> dict[str, Any] | None:
        """The object, or None if it doesn't exist. Existence is checked by listing the
        key, because S3 answers a read of a missing object with AccessDenied, not
        NoSuchKey, when the reader may list only some prefixes; a real refusal still
        raises."""
        listed = self.client.list_objects_v2(Bucket=self.bucket, Prefix=key, MaxKeys=1)
        if not any(item["Key"] == key for item in listed.get("Contents", [])):
            return None
        return self._read(key)

    def put(self, key: str, body: Mapping[str, Any]) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(body, indent=2).encode(),
            ContentType="application/json",
        )

    def _read(self, key: str) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(
            self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        )
        return data


class LocalStore:
    """This machine's records; reports go to evals/results/drift/."""

    def __init__(self, directory: Path = records.RECORDS_DIR, out: Path = LOCAL_DIR) -> None:
        self.directory, self.out = directory, out

    def records(self, prefix: str) -> list[dict[str, Any]]:
        if prefix.startswith("runs/"):
            kind = prefix.split("/")[1]
            return [r for r in records.load_all(self.directory) if r.get("kind") == kind]
        return []

    def get(self, key: str) -> dict[str, Any] | None:
        path = self.out / key.removeprefix("drift/")
        return json.loads(path.read_text()) if path.exists() else None

    def put(self, key: str, body: Mapping[str, Any]) -> None:
        path = self.out / key.removeprefix("drift/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, indent=2) + "\n")


SpanQuery = Callable[[str, datetime, datetime], list[dict[str, str]]]


def insights_query(client: Any = None, timeout_s: float = 60) -> SpanQuery:
    """Runs a Logs Insights query over aws/spans and returns its rows."""
    if client is None:
        import boto3

        client = boto3.client("logs")

    def query(text: str, start: datetime, end: datetime) -> list[dict[str, str]]:
        query_id = client.start_query(
            logGroupName=LOG_GROUP,
            startTime=int(start.timestamp()),
            endTime=int(end.timestamp()),
            queryString=text,
            limit=1000,
        )["queryId"]
        deadline = time.monotonic() + timeout_s
        while True:
            result = client.get_query_results(queryId=query_id)
            if result["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
                break
            if time.monotonic() > deadline:
                raise TimeoutError(f"Logs Insights query {query_id} didn't finish")
            time.sleep(1)
        if result["status"] != "Complete":
            raise RuntimeError(f"Logs Insights query {query_id}: {result['status']}")
        return [{f["field"]: f["value"] for f in row} for row in result["results"]]

    return query


# ---------------------------------------------------------------- the checks


def started(record: Mapping[str, Any]) -> datetime:
    """When a run started, in UTC; backfilled records hold only a date."""
    when = datetime.fromisoformat(str(record["started_at"]).replace("Z", "+00:00"))
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def check_models(
    monitor: Sequence[Mapping[str, Any]], calibration: Sequence[Mapping[str, Any]], since: datetime
) -> tuple[dict[str, Any], list[Signal]]:
    """A model version in this week's runs that no earlier run used."""
    groups = {
        "answer": ([r for r in monitor if r.get("suite") != "standby"], "model"),
        "standby": ([r for r in monitor if r.get("suite") == "standby"], "model"),
        "judge": (list(calibration), "judge_model"),
    }
    report: dict[str, Any] = {}
    signals: list[Signal] = []
    for name, (runs, field) in groups.items():
        before: set[str] = set()
        now: set[str] = set()
        for r in runs:
            seen = set((r.get("measured") or {}).get(field) or {})
            (now if started(r) >= since else before).update(seen)
        new = sorted(now - before) if before else []
        report[name] = {"this_week": sorted(now), "before": sorted(before), "new": new}
        if new:
            signals.append(
                Signal(
                    "models", "act", f"The {name} model is now {', '.join(new)}, not seen before"
                )
            )
    return report, signals


def check_sources(
    ingests: Sequence[Mapping[str, Any]] | None, today: date, now: datetime
) -> tuple[dict[str, Any], list[Signal]]:
    """What the latest ingest changed, and the golden cases that cite a changed source."""
    signals: list[Signal] = []
    golden = load().cases
    overdue = [
        {"case": c.id, "review_by": c.review_by.isoformat()}
        for c in golden
        if c.review_by and c.review_by <= today
    ]
    if overdue:
        names = ", ".join(o["case"] for o in overdue)
        signals.append(Signal("sources", "act", f"Golden cases past their review date: {names}"))
    if ingests is None:
        return {
            "ingest": "not checked: ingest records are only in AWS",
            "overdue": overdue,
        }, signals
    full = [r for r in ingests if not r.get("partial")]
    if not full:
        signals.append(Signal("sources", "act", "No record of an ingest run"))
        return {"ingest": None, "changed": [], "cases_to_review": [], "overdue": overdue}, signals
    latest = max(full, key=started)
    if now - started(latest) > WEEK + timedelta(days=1):
        signals.append(
            Signal("sources", "act", f"The last ingest run on record was {latest['started_at']}")
        )
    changed = [
        {k: s.get(k) for k in ("source_id", "status", "added", "removed", "sections_added")}
        for s in latest.get("sources", [])
        if s.get("added") or s.get("removed") or s.get("status") == "failed"
    ]
    for source_id, n in (latest.get("pruned") or {}).items():
        changed.append({"source_id": source_id, "status": "removed", "removed": n})
    ids = {c["source_id"] for c in changed}
    review: list[dict[str, Any]] = [
        {"case": c.id, "sources": sorted(ids & set(c.expect.sources))}
        for c in golden
        if ids & set(c.expect.sources)
    ]
    if review:
        names = ", ".join(r["case"] for r in review)
        signals.append(
            Signal("sources", "act", f"Sources changed; check these golden cases: {names}")
        )
    elif changed:
        signals.append(Signal("sources", "watch", f"Sources changed: {', '.join(sorted(ids))}"))
    report = {
        "ingest": latest["id"],
        "changed": changed,
        "cases_to_review": review,
        "overdue": overdue,
    }
    return report, signals


def new_low(series: Sequence[float]) -> bool:
    """The last value is below every earlier one, with enough earlier ones to say so."""
    return len(series) > LOW_AFTER and series[-1] < min(series[:-1])


def check_quality(
    monitor: Sequence[Mapping[str, Any]], calibration: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, Any], list[Signal]]:
    signals: list[Signal] = []
    full = sorted((r for r in monitor if r.get("suite") == "full"), key=started)
    means = [((r.get("summary") or {}).get("means") or {}) for r in full]
    for measure in sorted({m for run in means for m in run}):
        series = [run[measure] for run in means if measure in run]
        if new_low(series):
            signals.append(
                Signal("quality", "watch", f"{measure} {series[-1]:.3f}: the full run's lowest yet")
            )
    cal = sorted(calibration, key=started)
    agreements = [((r.get("summary") or {}).get("agreement") or {}) for r in cal]
    for judge in sorted({j for run in agreements for j in run}):
        series = [run[judge] for run in agreements if run.get(judge) is not None]
        if new_low(series):
            signals.append(
                Signal(
                    "quality", "watch", f"{judge} judge agreement {series[-1]:.3f}: its lowest yet"
                )
            )
    report = {
        "full_runs": [
            {"id": r["id"], "passed": r.get("passed"), "means": m}
            for r, m in zip(full, means, strict=True)
        ][-8:],
        "calibration": [
            {"id": r["id"], "agreement": a} for r, a in zip(cal, agreements, strict=True)
        ][-14:],
    }
    return report, signals


def psi(baseline: Mapping[str, int], current: Mapping[str, int], smoothing: float = 0.5) -> float:
    """The population stability index between two sets of category counts. Each count
    gets `smoothing` added, so a category missing from one week doesn't divide by zero."""
    categories = set(baseline) | set(current)
    if not categories:
        return 0.0
    total_b = sum(baseline.values()) + smoothing * len(categories)
    total_c = sum(current.values()) + smoothing * len(categories)
    value = 0.0
    for c in categories:
        b = (baseline.get(c, 0) + smoothing) / total_b
        a = (current.get(c, 0) + smoothing) / total_c
        value += (a - b) * math.log(a / b)
    return round(value, 4)


def counts(rows: Iterable[Mapping[str, str]]) -> dict[str, int]:
    return {str(r.get("category")): int(float(r.get("n", 0))) for r in rows if "category" in r}


def check_mix(
    query: SpanQuery | None, store: Store, now: datetime
) -> tuple[dict[str, Any], list[Signal]]:
    """Visitors' questions this week against the baseline week."""
    if query is None:
        return {"status": "not checked: the spans are only in AWS"}, []
    start = max(now - WEEK, MIX_SINCE)
    # Logs Insights refuses a window that ends before it starts.
    week = {
        name: counts(query(text, start, now)) if start < now else {}
        for name, text in MIX_QUERIES.items()
    }
    questions = sum(week["outcomes"].values())
    report: dict[str, Any] = {
        "from": start.isoformat(),
        "to": now.isoformat(),
        "questions": questions,
    }
    if questions < MIX_MINIMUM:
        report["status"] = (
            f"not compared: {questions} visitors' questions, fewer than {MIX_MINIMUM}"
        )
        return report, []
    baseline = store.get("drift/baseline.json")
    if baseline is None:
        store.put(
            "drift/baseline.json",
            {"from": start.isoformat(), "to": now.isoformat(), "counts": week},
        )
        report["status"] = "baseline set: this week is the baseline"
        return report, []
    scores = {name: psi(baseline["counts"].get(name, {}), week[name]) for name in MIX_QUERIES}
    report.update(status="compared", baseline_from=baseline["from"], psi=scores, counts=week)
    signals = []
    for name, value in scores.items():
        if value >= PSI_ACT:
            signals.append(Signal("mix", "act", f"Questions' {name} shifted a lot: PSI {value}"))
        elif value >= PSI_WATCH:
            signals.append(Signal("mix", "watch", f"Questions' {name} shifted: PSI {value}"))
    return report, signals


# ---------------------------------------------------------------- the report


def report(store: Store, query: SpanQuery | None, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    since = now - WEEK
    monitor = store.records("runs/monitor/") + store.records("runs/post-deploy/")
    calibration = store.records("runs/calibration/")
    ingests = None if isinstance(store, LocalStore) else store.records("ingest/")
    sections: dict[str, Any] = {}
    signals: list[Signal] = []
    for name, (section, found) in {
        "models": check_models(monitor, calibration, since),
        "sources": check_sources(ingests, now.date(), now),
        "quality": check_quality(monitor, calibration),
        "mix": check_mix(query, store, now),
    }.items():
        sections[name] = section
        signals += found
    return {
        "id": f"{now.strftime('%Y%m%dT%H%M%S')}Z-drift",
        "kind": "drift",
        "generated_at": now.isoformat(),
        "since": since.isoformat(),
        "act": sum(s.level == "act" for s in signals),
        "signals": [asdict(s) for s in signals],
        **sections,
    }


def emit(act: int, client: Any = None) -> None:
    """The number of signals to act on, for the drift alarm."""
    if client is None:
        import boto3

        client = boto3.client("cloudwatch")
    client.put_metric_data(
        Namespace=NAMESPACE, MetricData=[{"MetricName": METRIC, "Value": act, "Unit": "Count"}]
    )


def main(
    argv: Sequence[str],
    store: Store | None = None,
    query: SpanQuery | None = None,
    metric: Callable[[int], None] | None = None,
    now: datetime | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.drift")
    parser.parse_args(argv)
    bucket = os.environ.get("EVAL_RECORDS_BUCKET")
    if store is None:
        store = S3Store(bucket) if bucket else LocalStore()
        if bucket and query is None:
            query = insights_query()
    found = report(store, query, now)
    store.put(f"drift/{found['id']}.json", found)
    store.put("drift/latest.json", found)
    for s in found["signals"]:
        print(f"{s['level'].upper():5} {s['check']}: {s['summary']}")
    print(f"Mix: {found['mix'].get('status')}")
    print(f"Drift: {found['act']} to act on, {len(found['signals']) - found['act']} to watch")
    if metric is not None:
        metric(found["act"])
    elif bucket:
        emit(found["act"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
