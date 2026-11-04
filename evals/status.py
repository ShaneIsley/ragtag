"""The status page's snapshot: what /status shows, built from the run records, the drift
report and the app's spans, and written to status/snapshot.json in the records bucket.

The eval function rebuilds it after every scheduled run (evals/lambda_handler.py), so
it's at most a day old; the page adds the alarms' live states itself. Everything in it
is an aggregate. Visitors' counts from 1 to 4 become "fewer than 5" here, before the
snapshot is written, so a small count never reaches the page. The evaluation's own
requests are left out of usage (app.synthetic), as they are from the drift checks.

Run it locally with `make status`, which writes evals/results/status/snapshot.json from
this machine's records; the app shows that file when STATUS_SNAPSHOT_FILE names it.
"""

import argparse
import itertools
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from evals import drift, records
from evals.drift import LocalStore, S3Store, SpanQuery, Store, insights_query, started
from evals.golden import Case, load
from evals.promptfoo_tests import runnable as can_run

SNAPSHOT_KEY = "status/snapshot.json"
LOCAL_DIR = Path(__file__).resolve().parents[1] / "evals" / "results" / "status"
SUPPRESS_BELOW = 5
SYDNEY = ZoneInfo("Australia/Sydney")
THRESHOLDS_FILE = Path(__file__).with_name("thresholds.yaml")
MEASURES = {
    "facts": "Expected facts stated",
    "groundedness": "Grounded in the passages",
    "relevance": "Relevant to the question",
    "recall": "Expected sources found",
    "cites_expected": "Expected sources cited",
}
VISITORS = "not ispresent(attributes.app.synthetic)"
USAGE_QUERIES = {
    "outcomes": f"""
        filter name = "answer" and {VISITORS}
        | fields coalesce(attributes.app.decline_reason, "answered") as category
        | stats count(*) as n by category""",
    "masked": f"""
        filter name = "mask" and {VISITORS} and ispresent(attributes.app.masked)
        | stats count(*) as n""",
    "failovers": f"""
        filter name = "answer" and {VISITORS} and attributes.app.provider = "bedrock"
            and attributes.app.requested_provider = "auto"
        | stats count(*) as n""",
}


def runnable(cases: Sequence[Case]) -> list[Case]:
    return [c for c in cases if can_run(c)]


def shown(n: int) -> int | str:
    """A visitors' count as the page may show it."""
    return "fewer than 5" if 0 < n < SUPPRESS_BELOW else n


def day(record: Mapping[str, Any]) -> str:
    """The day a run started, in Sydney, as the page shows it: "2 Nov"."""
    when = started(record).astimezone(SYDNEY)
    return f"{when.day} {when:%b}"


def latest(runs: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    return max(runs, key=started) if runs else None


RELEASES_SHOWN = 6
_RULE_FAILURE = re.compile(r"^rule failed: (\S+) (\w+):")


def finished(record: Mapping[str, Any]) -> datetime:
    return started(record) + timedelta(seconds=float(record.get("duration_s") or 0))


def when(moment: datetime) -> str:
    """A time in Sydney, as the releases show it: "3 Nov, 4:39 pm"."""
    local = moment.astimezone(SYDNEY)
    return f"{local.day} {local:%b}, {local:%-I:%M %p}".replace("AM", "am").replace("PM", "pm")


def failure(record: Mapping[str, Any]) -> str | None:
    """A failed run's first failure, briefly: the case and check, or the measure."""
    failures = record.get("failures") or []
    if not failures:
        return None
    first = str(failures[0])
    m = _RULE_FAILURE.match(first)
    return f"{m.group(1)} ({m.group(2)})" if m else first.split(":")[0]


def releases(
    gate: Sequence[Mapping[str, Any]], production: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Each version, newest first: the gate's runs on it, and the first check of it on
    production. A version the gate stopped never reached production, so it has none.

    The version is the commit the app reports on /health. Whether the answer path changed
    comes from each version's fingerprint of the answer files (evals/records.py), against
    the nearest older version that has one. A version without one skipped the gate, which
    runs whenever the answer files change, so it changed none of them.
    """
    versions: dict[str, dict[str, list[Mapping[str, Any]]]] = {}
    for kind, runs in (("gate", gate), ("production", production)):
        for r in runs:
            version = (r.get("measured") or {}).get("version")
            if version:
                versions.setdefault(version, {"gate": [], "production": []})[kind].append(r)
    rows: list[dict[str, Any]] = []
    for version, found in versions.items():
        tries = sorted(found["gate"], key=started)
        after = sorted(found["production"], key=started)
        first = after[0] if after else None
        moment = finished(first) if first else finished(tries[-1])
        notes = [str(r["note"]) for r in (*tries, *after) if r.get("note")]
        fingerprints = [(r.get("measured") or {}).get("fingerprint") for r in (*tries, *after)]
        rows.append(
            {
                "version": version[:7],
                "moment": moment,
                "when": when(moment),
                "note": notes[0] if notes else None,
                "fingerprint": next((f for f in fingerprints if f), None),
                "gate": [
                    {
                        "passed": bool(r.get("passed")),
                        "failure": failure(r),
                        "failures": len(r.get("failures") or []),
                        "cases": (r.get("summary") or {}).get("tests"),
                    }
                    for r in tries
                ],
                "released": first is not None,
                "after": None
                if first is None
                else {
                    "passed": bool(first.get("passed")),
                    "what": "Post-deploy check"
                    if first.get("kind") == "post-deploy"
                    else "Full check",
                    "time": when(finished(first)).split(", ")[1],
                },
            }
        )
    rows.sort(key=lambda row: row["moment"], reverse=True)
    for i, row in enumerate(rows):
        older = next((r["fingerprint"] for r in rows[i + 1 :] if r["fingerprint"]), None)
        both = row["fingerprint"] and older
        row["answer_path_changed"] = row["fingerprint"] != older if both else None
    for row in rows:
        del row["moment"], row["fingerprint"]
    return rows[:RELEASES_SHOWN]


def means(record: Mapping[str, Any]) -> dict[str, float]:
    found = (record.get("summary") or {}).get("means") or {}
    return {k: round(float(v), 3) for k, v in found.items() if k in MEASURES}


def failed_cases(record: Mapping[str, Any]) -> set[str]:
    return {case for case, _ in (record.get("summary") or {}).get("failed_checks") or []}


def quality(monitor: Sequence[Mapping[str, Any]], thresholds: Mapping[str, Any]) -> dict[str, Any]:
    full = sorted((r for r in monitor if r.get("suite") == "full"), key=started)[-12:]
    canary = sorted((r for r in monitor if r.get("suite") == "canary"), key=started)[-14:]
    minimums = {k: v.get("minimum") for k, v in thresholds["measures"].items()}
    markers = []
    for before, after in itertools.pairwise(full):
        version = (after.get("measured") or {}).get("version")
        if version and version != (before.get("measured") or {}).get("version"):
            markers.append({"day": day(after), "what": f"release {version[:7]}"})
        models = set((after.get("measured") or {}).get("model") or {})
        if models - set((before.get("measured") or {}).get("model") or {}):
            markers.append({"day": day(after), "what": f"model {', '.join(sorted(models))}"})
    last = latest(full)
    return {
        "measures": [
            {"name": k, "label": label, "minimum": minimums.get(k)} for k, label in MEASURES.items()
        ],
        "full_runs": [
            {"day": day(r), "passed": bool(r.get("passed")), "means": means(r)} for r in full
        ],
        "latest_full": None
        if last is None
        else {
            "day": day(last),
            "passed": bool(last.get("passed")),
            "cases": (last.get("summary") or {}).get("tests"),
            "means": means(last),
            "latency_p90_s": (last.get("summary") or {}).get("latency_p90_s"),
            "warnings": len(last.get("warnings") or []),
            "failures": len(last.get("failures") or []),
        },
        "canary": [{"day": day(r), "passed": bool(r.get("passed"))} for r in canary],
        "markers": markers,
        "latency_p90_maximum_s": thresholds["latency"]["p90_seconds_maximum"],
    }


def judges(calibration: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    runs = sorted(calibration, key=started)[-14:]
    return {
        "runs": [
            {
                "day": day(r),
                "passed": bool(r.get("passed")),
                "agreement": {
                    j: round(a, 3)
                    for j, a in ((r.get("summary") or {}).get("agreement") or {}).items()
                    if a is not None
                },
                "model": sorted((r.get("measured") or {}).get("judge_model") or {}),
            }
            for r in runs
        ]
    }


def standby(monitor: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    last = latest([r for r in monitor if r.get("suite") == "standby"])
    if last is None:
        return None
    return {
        "day": day(last),
        "passed": bool(last.get("passed")),
        "cases": (last.get("summary") or {}).get("tests"),
        "means": means(last),
        "model": sorted((last.get("measured") or {}).get("model") or {}),
    }


def usage(query: SpanQuery | None, now: datetime) -> dict[str, Any] | None:
    """Visitors' questions over the last 7 and 30 days, from the spans."""
    if query is None:
        return None
    windows: dict[str, Any] = {}
    for days in (7, 30):
        start = max(now - timedelta(days=days), drift.MIX_SINCE)

        def rows(name: str, start: datetime = start) -> list[dict[str, str]]:
            # Logs Insights refuses a window that ends before it starts.
            return query(USAGE_QUERIES[name], start, now) if start < now else []

        outcomes = drift.counts(rows("outcomes"))
        single = {
            name: sum(int(float(row.get("n", 0))) for row in rows(name))
            for name in ("masked", "failovers")
        }
        total = sum(outcomes.values())
        answered = outcomes.get("answered", 0)
        windows[f"days_{days}"] = {
            "from": start.astimezone(SYDNEY).strftime("%-d %b"),
            "questions": shown(total),
            "answered": shown(answered),
            "declined": shown(total - answered),
            "by_outcome": {k: shown(v) for k, v in sorted(outcomes.items())},
            "masked": shown(single["masked"]),
            "failovers": shown(single["failovers"]),
        }
    return windows


def golden_coverage() -> dict[str, Any]:
    cases = load().cases
    run = runnable(cases)
    return {
        "cases": len(cases),
        "runnable": len(run),
        "by_category": dict(sorted(Counter(c.category for c in run).items())),
        "by_audience": dict(sorted(Counter(c.audience for c in run).items())),
    }


# In the order the page lists them.
AUDIENCES = {
    "staff": "staff",
    "student": "students",
    "researcher": "researchers",
    "any": "questions for anyone",
}


def same_golden_set(record: Mapping[str, Any] | None) -> bool:
    """Whether a run asked today's golden set, so its cases can be counted from it."""
    if record is None:
        return False
    used = (record.get("measured_with") or {}).get("golden_set")
    return bool(used) and used == records.file_hash(("evals/golden.yaml",))


def by_audience(record: Mapping[str, Any] | None) -> dict[str, dict[str, int]]:
    """For each audience, how many of the full run's cases had no failed check."""
    if record is None or not same_golden_set(record):
        return {}
    failed = failed_cases(record)
    result: dict[str, dict[str, int]] = {}
    for c in runnable(load().cases):
        group = result.setdefault(AUDIENCES.get(c.audience, c.audience), {"cases": 0, "clean": 0})
        group["cases"] += 1
        group["clean"] += c.id not in failed
    order = list(AUDIENCES.values())
    return dict(sorted(result.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 99))


def injections(record: Mapping[str, Any] | None) -> tuple[int, int] | None:
    if record is None or not same_golden_set(record):
        return None
    cases = [c.id for c in runnable(load().cases) if c.category == "injection"]
    failed = failed_cases(record)
    return sum(c not in failed for c in cases), len(cases)


def assurance(snap: Mapping[str, Any], full: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """The AI Assurance Framework's seven parts, each with what's measured now."""
    q = snap["quality"]
    last = q["latest_full"]
    use30 = (snap.get("usage") or {}).get("days_30")
    std = snap.get("standby")
    judged = snap["judges"]["runs"]
    audience = by_audience(full)
    injected = injections(full)

    def run_text() -> str:
        if last is None:
            return "No full evaluation run yet."
        m = last["means"]
        return (
            f"The latest full run, on {last['day']}, {'passed' if last['passed'] else 'failed'}: "
            f"{m.get('facts', 0):.0%} of expected facts stated, "
            f"{m.get('groundedness', 0):.0%} grounded in the passages."
        )

    parts = [
        (
            "Business value",
            "A measurable benefit, at a cost the budget owner approves",
            f"Visitors' questions answered in the last 30 days: {use30['answered']}."
            if use30
            else "Visitors' questions are counted on the deployed site.",
            "About A$5 to 8 a month to run, with budget alerts.",
        ),
        (
            "Sensitive data",
            "Use only data the system is rated for",
            f"Visitors' questions with personal details removed in the last 30 days: "
            f"{use30['masked']}."
            if use30
            else "Personal details are removed before a question goes anywhere.",
            "No question or answer is stored. The sources are public.",
        ),
        (
            "Harm",
            "Avoid incorrect advice; monitor and test",
            run_text(),
            f"{injected[0]} of {injected[1]} prompt-injection cases resisted."
            if injected
            else "Prompt injection is tested in every full run.",
        ),
        (
            "Fairness",
            "Data that represents the people it serves",
            "Test cases by audience in the last full run, without a failed check: "
            + "; ".join(f"{a} {g['clean']} of {g['cases']}" for a, g in audience.items())
            + "."
            if audience
            else "The test cases cover staff, students and researchers.",
            "Sources are UNSW's own published rules for all three groups.",
        ),
        (
            "Transparency and accountability",
            "Explain how outputs are produced; human oversight",
            f"Expected sources cited in {last['means'].get('cites_expected', 0):.0%} of answers "
            "in the last full run."
            if last
            else "Every answer cites its passages.",
            "Every answer shows its sources, which model wrote it, and a trace ID.",
        ),
        (
            "Resilience and safety",
            "Secure, monitored and able to fail safely",
            f"The failover model's last check on {std['day']} "
            f"{'passed' if std['passed'] else 'failed'}."
            if std
            else "The failover model is checked weekly.",
            "The judges' agreement with known verdicts last night: "
            + ", ".join(f"{j} {a:.0%}" for j, a in judged[-1]["agreement"].items())
            + "."
            if judged
            else "The judges are checked nightly against fixed answers.",
        ),
        (
            "Compliance",
            "UNSW policy, privacy and the law",
            "Data stays in Australia, except the answer model's global processing, which is "
            "recorded as an exception.",
            "A privacy impact assessment is done. The site shuts down at the end of November "
            "2026, and everything is then deleted.",
        ),
    ]
    return [
        {"part": p, "asks": asks, "signal": signal, "also": also} for p, asks, signal, also in parts
    ]


def build(store: Store, query: SpanQuery | None, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    thresholds = yaml.safe_load(THRESHOLDS_FILE.read_text())
    # Production runs only, scheduled or by hand, as the trend counts them.
    monitor = store.records("runs/monitor/") + store.records("runs/post-deploy/")
    calibration = store.records("runs/calibration/")
    gate = [r for r in store.records("runs/release/") if r.get("suite") == "full"]
    report = store.get("drift/latest.json")
    full = latest([r for r in monitor if r.get("suite") == "full"])
    snap: dict[str, Any] = {
        "generated_at": now.isoformat(),
        "generated": now.astimezone(SYDNEY).strftime("%-d %b %Y, %-I:%M %p"),
        "quality": quality(monitor, thresholds),
        "judges": judges(calibration),
        "standby": standby(monitor),
        "drift": None
        if report is None
        else {
            "day": day({"started_at": report["generated_at"]}),
            "signals": report.get("signals", []),
            "cases_to_review": (report.get("sources") or {}).get("cases_to_review", []),
            "models": {k: v.get("this_week") for k, v in (report.get("models") or {}).items()},
            "mix": (report.get("mix") or {}).get("status"),
        },
        # The reasons without their notes on which runs showed them, which mean
        # nothing to a visitor: "Gives the Harvard format ... (runs 5 and 8)".
        "known_issues": [
            {**k, "why": re.sub(r"\s*\([^)]*\)$", "", str(k.get("why", "")))}
            for k in thresholds.get("known_issues", [])
        ],
        "golden": golden_coverage(),
        "usage": usage(query, now),
        "releases": releases(gate, monitor),
    }
    snap["assurance"] = assurance(snap, full)
    return snap


def publish(store: Store | None = None, query: SpanQuery | None = None) -> dict[str, Any]:
    """Builds the snapshot and writes it: to the bucket on AWS, or locally."""
    bucket = os.environ.get("EVAL_RECORDS_BUCKET")
    if store is None:
        store = S3Store(bucket) if bucket else LocalStore()
        if bucket and query is None:
            query = insights_query()
    snap = build(store, query)
    if bucket or not isinstance(store, LocalStore):
        store.put(SNAPSHOT_KEY, snap)
    else:
        LOCAL_DIR.mkdir(parents=True, exist_ok=True)
        (LOCAL_DIR / "snapshot.json").write_text(json.dumps(snap, indent=2) + "\n")
    return snap


def main(argv: Sequence[str]) -> int:
    argparse.ArgumentParser(prog="python -m evals.status").parse_args(argv)
    snap = publish()
    where = "s3" if os.environ.get("EVAL_RECORDS_BUCKET") else str(LOCAL_DIR / "snapshot.json")
    print(f"Status snapshot written ({where}), {snap['generated']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
