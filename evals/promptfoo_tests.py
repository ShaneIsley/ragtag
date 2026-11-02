"""promptfoo test cases, made from the golden set.

Each runnable case becomes one test: its last question, plus the checks in
evals/assertions.py. Cases that need a capability the app doesn't have yet are
skipped, and the run says how many. EVAL_SUITE picks the cases: full (the default),
canary or standby (evals/suites.yaml).
"""

import os
from datetime import date
from typing import Any

from evals.golden import Capability, Case, load, overdue
from evals.suites import SUITES, Suite, chosen

# Capabilities the app has. Conversation memory arrives in Phase 2 step 5.
AVAILABLE: frozenset[Capability] = frozenset()

# Per-case minimums for the measures. They stay None: one case's score says little,
# so the baseline's minimums apply to each run's averages instead (evals/gate.py,
# evals/thresholds.yaml).
RECALL_MINIMUM: float | None = None
CITES_EXPECTED_MINIMUM: float | None = None
# Groundedness and relevance minimums are on the graders' 1-to-5 scale.
GROUNDEDNESS_MINIMUM: float | None = None
RELEVANCE_MINIMUM: float | None = None
LATENCY_MAXIMUM_MS: float | None = None


def _check(name: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    check: dict[str, Any] = {"type": "python", "value": f"file://assertions.py:{name}"}
    check["metric"] = name
    if config:
        check["config"] = config
    return check


def runnable(case: Case) -> bool:
    return set(case.requires) <= AVAILABLE


def to_test(case: Case) -> dict[str, Any]:
    checks = [
        _check("outcome"),
        _check("toxicity"),
        _check("latency", {"maximum_ms": LATENCY_MAXIMUM_MS}),
    ]
    if case.expect.must_not_contain:
        checks.append(_check("forbidden_text"))
    if case.category == "personal_details":
        checks.append(_check("masking"))
    if case.expect.facts:
        checks.append({**_check("check_facts"), "metric": "facts"})
    if case.expect.outcome == "answer":
        checks.append(_check("groundedness", {"minimum": GROUNDEDNESS_MINIMUM}))
        checks.append(_check("relevance", {"minimum": RELEVANCE_MINIMUM}))
    if case.expect.sources:
        checks.append(_check("recall", {"minimum": RECALL_MINIMUM}))
        checks.append(_check("cites_expected", {"minimum": CITES_EXPECTED_MINIMUM}))
    return {
        "description": f"{case.id} ({case.category})",
        "vars": {"question": case.last_question, "case_id": case.id},
        "metadata": {"case_id": case.id, "category": case.category, "audience": case.audience},
        "assert": checks,
    }


def suite_from_env() -> Suite:
    suite = os.environ.get("EVAL_SUITE") or "full"
    if suite not in SUITES:
        raise ValueError(f"EVAL_SUITE must be one of {', '.join(SUITES)}, not {suite!r}")
    return suite


def generate_tests(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    golden = load()
    suite = suite_from_env()
    skipped = [c.id for c in golden.cases if not runnable(c)]
    if skipped and suite == "full":
        print(f"Skipping {len(skipped)} cases that need capabilities not built yet.")
    for case in overdue(golden, date.today()):
        print(f"Review due: {case.id} (review_by {case.review_by}). Check its facts.")
    return [to_test(c) for c in golden.cases if runnable(c) and chosen(suite, c.id)]
