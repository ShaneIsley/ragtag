"""The run-level gate: does a whole run meet the baseline?

promptfoo enforces the rules case by case (outcome, forbidden text, toxicity, no
contradicted fact). The measures pass case by case, because one case's score says
little; this averages each measure over the run and compares the average with its
minimum in thresholds.yaml, set from the baseline. A run fails if any rule failed,
any case errored, any measure is below its minimum, or the 90th-percentile latency is
over its maximum.

Which suite ran decides how the measures count. The full suite applies them. A
canary's dozen cases are too few for minimums set from full runs, so a measure below
one only warns, and the weekly full run decides. A standby run asks the failover
model, which the minimums weren't set for, so its measures are recorded only. Rules
fail the run in every suite.

Every run is recorded (evals/records.py), with its kind and suite.

    python -m evals.gate evals/results/latest.json [--kind release] [--suite full]
"""

import argparse
import json
import os
import statistics
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from evals import records
from evals.golden import load
from evals.suites import SUITES, Suite

THRESHOLDS_FILE = Path(__file__).with_name("thresholds.yaml")
RULES = frozenset({"outcome", "forbidden_text", "toxicity", "facts", "masking"})
MEASURES = ("facts", "groundedness", "relevance", "recall", "cites_expected")
NOT_APPLICABLE = "not applicable to this case"


@dataclass
class Summary:
    tests: int = 0
    rule_failures: list[str] = field(default_factory=list)
    # (case, check) for each rule failure, to match against known issues.
    failed_checks: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    means: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    latency_p90_s: float = 0.0


def p90(values: Sequence[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, round(0.9 * len(ordered)) - 1)] if ordered else 0.0


def summarise(
    results: Mapping[str, Any],
    categories: Mapping[str, str],
    excluded: Mapping[str, frozenset[str]] | None = None,
) -> Summary:
    """Averages each measure over the cases it applies to, leaving out the categories
    `excluded` names for it, and collects rule failures, errors and latencies."""
    excluded = excluded or {}
    summary = Summary()
    scores: dict[str, list[float]] = {m: [] for m in MEASURES}
    latencies: list[float] = []
    for row in results["results"]["results"]:
        summary.tests += 1
        case_id = str(row["vars"]["case_id"])
        category = categories.get(case_id, "")
        grading = row.get("gradingResult")
        if not grading:
            summary.errors.append(f"{case_id}: {row.get('error') or 'no result'}")
            continue
        for check in grading.get("componentResults", []):
            metric = check["assertion"].get("metric")
            if metric in RULES and not check["pass"]:
                summary.rule_failures.append(f"{case_id} {metric}: {check['reason']}")
                summary.failed_checks.append((case_id, str(metric)))
            applies = check.get("reason") != NOT_APPLICABLE
            if metric in scores and applies and category not in excluded.get(metric, frozenset()):
                scores[metric].append(float(check["score"]))
        metadata = (row.get("response") or {}).get("metadata") or {}
        if "latency_ms" in metadata:
            latencies.append(float(metadata["latency_ms"]) / 1000)
    summary.means = {m: statistics.mean(v) for m, v in scores.items() if v}
    summary.counts = {m: len(v) for m, v in scores.items()}
    summary.latency_p90_s = p90(latencies)
    return summary


def known(thresholds: Mapping[str, Any]) -> set[tuple[str, str]]:
    return {(str(k["case"]), str(k["check"])) for k in thresholds.get("known_issues") or []}


def warnings(summary: Summary, thresholds: Mapping[str, Any]) -> list[str]:
    """Rule failures that are listed as known issues: reported, but not failing."""
    listed = known(thresholds)
    return [
        f"known issue: {text}"
        for key, text in zip(summary.failed_checks, summary.rule_failures, strict=True)
        if key in listed
    ]


def rule_failures(summary: Summary, thresholds: Mapping[str, Any]) -> list[str]:
    listed = known(thresholds)
    found = [
        f"rule failed: {text}"
        for key, text in zip(summary.failed_checks, summary.rule_failures, strict=True)
        if key not in listed
    ]
    return found + [f"error: {e}" for e in summary.errors]


def failures(summary: Summary, thresholds: Mapping[str, Any]) -> list[str]:
    """What fails a full run: rule failures, errors, and the run-level limits."""
    return rule_failures(summary, thresholds) + below_limits(summary, thresholds)


def verdict(
    summary: Summary, thresholds: Mapping[str, Any], suite: Suite
) -> tuple[list[str], list[str]]:
    """(failures, warnings) for a run of `suite`; see the module docstring."""
    found = rule_failures(summary, thresholds)
    warned = warnings(summary, thresholds)
    limits = below_limits(summary, thresholds)
    if suite == "full":
        found += limits
    elif suite == "canary":
        warned += [f"{text} (canary: the weekly full run decides)" for text in limits]
    return found, warned


def below_limits(summary: Summary, thresholds: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    for name, limits in thresholds["measures"].items():
        mean = summary.means.get(name)
        if mean is None:
            found.append(f"{name}: no scores in this run")
        elif mean < limits["minimum"]:
            found.append(f"{name}: mean {mean:.3f} is below the minimum {limits['minimum']}")
    maximum = thresholds["latency"]["p90_seconds_maximum"]
    if summary.latency_p90_s > maximum:
        found.append(f"latency: 90th percentile {summary.latency_p90_s:.1f} s is over {maximum} s")
    return found


def excluded_categories(thresholds: Mapping[str, Any]) -> dict[str, frozenset[str]]:
    return {
        name: frozenset(limits.get("exclude_categories", []))
        for name, limits in thresholds["measures"].items()
    }


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.gate")
    parser.add_argument("results", type=Path, help="promptfoo results JSON")
    parser.add_argument(
        "--kind", choices=records.KINDS, default=os.environ.get("EVAL_KIND") or None
    )
    parser.add_argument("--suite", choices=SUITES, default=os.environ.get("EVAL_SUITE") or "full")
    parser.add_argument("--note", help="why this run was made, for the record")
    parser.add_argument("--no-record", action="store_true", help="don't save a run record")
    args = parser.parse_args(argv)

    thresholds = yaml.safe_load(THRESHOLDS_FILE.read_text())
    results = json.loads(args.results.read_text())
    categories = {c.id: c.category for c in load().cases}
    summary = summarise(results, categories, excluded_categories(thresholds))
    print(f"{args.suite} suite: {summary.tests} tests; {len(summary.rule_failures)} rule failures")
    for name in MEASURES:
        if name in summary.means:
            limit = thresholds["measures"].get(name, {}).get("minimum", "none")
            mean = summary.means[name]
            print(f"  {name:15} {mean:.3f}  (n={summary.counts[name]}, minimum {limit})")
    p90_limit = thresholds["latency"]["p90_seconds_maximum"]
    print(f"  {'latency p90':15} {summary.latency_p90_s:.1f} s  (maximum {p90_limit} s)")
    found, warned = verdict(summary, thresholds, args.suite)
    for warning in warned:
        print(f"WARN {warning}")
    for failure in found:
        print(f"FAIL {failure}")
    print("Gate: FAILED" if found else "Gate: passed")

    if summary.tests and len(summary.errors) == summary.tests:
        print("Not recorded: no case got an answer from the app, so nothing was measured.")
    elif not args.no_record:
        target = os.environ.get("EVAL_TARGET_URL", "http://127.0.0.1:8001")
        provider = os.environ.get("EVAL_PROVIDER", "auto")
        record = records.build(
            results,
            asdict(summary),
            found,
            warned,
            kind=args.kind or records.default_kind(target, provider),
            suite=args.suite,
            target=target,
            provider=provider,
            version=records.served_version(target),
            note=args.note,
        )
        path = records.save(record)
        records.keep_results(args.results, path)
        print(f"Recorded as a {record['kind']} run: {path}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
