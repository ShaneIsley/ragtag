import hashlib
import json
from typing import Any

import httpx
import pytest

from evals import assertions, scoring
from evals.golden import load
from evals.promptfoo_tests import generate_tests, runnable
from evals.provider import ask

CASES = {c.id: c for c in load().cases}
ANSWERABLE = CASES["deepseek-restricted"]
OFF_TOPIC = CASES["capital-of-france"]
PERSONAL = CASES["phone-number-apa"]


def response(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "answer": "DeepSeek is restricted at UNSW [1].",
        "declined": False,
        "decline_reason": None,
        "sources": [{"number": 1, "source_id": "unsw-using-chatgpt-edu"}],
        "retrieved": [{"source_id": "unsw-using-chatgpt-edu"}, {"source_id": "unsw-myit-ai"}],
    }
    return base | fields


def test_outcome_passes_only_when_answer_or_decline_matches() -> None:
    assert scoring.check_outcome(ANSWERABLE, response()).passed
    assert not scoring.check_outcome(ANSWERABLE, response(declined=True)).passed
    declined = response(declined=True, decline_reason="no_match")
    assert scoring.check_outcome(OFF_TOPIC, declined).passed
    assert "declined (no_match)" in scoring.check_outcome(OFF_TOPIC, declined).reason


def test_forbidden_text_ignores_case() -> None:
    assert scoring.check_forbidden_text(PERSONAL, response()).passed
    leaked = response(answer="We'll call 0400 000 000 back. Reference it like this [1].")
    check = scoring.check_forbidden_text(PERSONAL, leaked)
    assert not check.passed and "0400 000 000" in check.reason


def test_recall_is_the_share_of_expected_sources_retrieved() -> None:
    case = CASES["new-ai-tool-before-use"]  # expects three sources
    retrieved = [{"source_id": "unsw-using-chatgpt-edu"}, {"source_id": "unsw-myit-ai"}]
    check = scoring.check_recall(case, response(retrieved=retrieved))
    assert check is not None
    assert check.score == pytest.approx(2 / 3)
    assert check.passed  # no threshold until the baseline
    assert "missing unsw-ig-policy-ai" in check.reason
    assert scoring.check_recall(OFF_TOPIC, response()) is None


def test_measures_fail_below_a_threshold_once_one_is_set() -> None:
    case = CASES["new-ai-tool-before-use"]
    check = scoring.check_recall(case, response(retrieved=[]), minimum=0.5)
    assert check is not None and not check.passed and "minimum 0.5" in check.reason


def test_cites_expected_is_a_hit_when_any_expected_source_is_cited() -> None:
    cited = [{"source_id": "unsw-using-chatgpt-edu"}, {"source_id": "unsw-code-of-conduct"}]
    check = scoring.check_cites_expected(ANSWERABLE, response(sources=cited))
    assert check is not None and check.score == 1.0
    miss = scoring.check_cites_expected(ANSWERABLE, response(sources=cited[1:]))
    assert miss is not None and miss.score == 0.0 and "cites none of" in miss.reason
    assert scoring.check_cites_expected(ANSWERABLE, response(declined=True)) is None


SAFE = {"hate": "safe", "sexual": "safe", "violence": "safe", "self_harm": "safe"}


def test_toxicity_fails_any_answer_rating_above_safe() -> None:
    assert scoring.check_toxicity(response(answer_safety=SAFE)).passed
    unsafe = scoring.check_toxicity(response(answer_safety=SAFE | {"violence": "low"}))
    assert not unsafe.passed and "violence low" in unsafe.reason


def test_toxicity_fails_a_blocked_answer_and_passes_fixed_declines() -> None:
    blocked = response(declined=True, decline_reason="answer_filtered", answer_safety=None)
    assert not scoring.check_toxicity(blocked).passed
    fixed = response(declined=True, decline_reason="no_match", answer_safety=None)
    assert scoring.check_toxicity(fixed).passed


def test_toxicity_reports_a_flagged_question_without_failing_on_it() -> None:
    flagged = {"flags": ["Possible attempt to change the service's rules"]}
    check = scoring.check_toxicity(response(answer_safety=SAFE, question_safety=flagged))
    assert check.passed and "question flagged" in check.reason


def test_latency_records_until_a_maximum_is_set() -> None:
    assert scoring.check_latency(12_000).passed
    assert not scoring.check_latency(12_000, maximum_ms=10_000).passed


def test_assertions_read_the_response_from_provider_metadata() -> None:
    context = {
        "vars": {"case_id": ANSWERABLE.id},
        "metadata": {"response": response(), "latency_ms": 2500.0},
        "config": {"minimum": None},
    }
    assert assertions.outcome("", context)["pass"]
    assert assertions.recall("", context)["score"] == 1.0
    assert assertions.latency("", context)["reason"] == "2.5 s"


def test_tests_cover_every_runnable_case_and_skip_conversations() -> None:
    tests = generate_tests()
    ids = {t["vars"]["case_id"] for t in tests}
    assert ids == {c.id for c in CASES.values() if runnable(c)}
    assert not any(CASES[i].category == "conversation" for i in ids)
    for t in tests:
        metrics = {a["metric"] for a in t["assert"]}
        assert {"outcome", "toxicity", "latency"} <= metrics
        case = CASES[t["vars"]["case_id"]]
        assert ("forbidden_text" in metrics) == bool(case.expect.must_not_contain)
        assert ("recall" in metrics) == bool(case.expect.sources)
        assert ("facts" in metrics) == bool(case.expect.facts)
        assert ("masking" in metrics) == (case.category == "personal_details")
        graded = case.expect.outcome == "answer"
        assert ("groundedness" in metrics) == graded and ("relevance" in metrics) == graded


def test_provider_signs_the_body_and_returns_the_full_response() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["hash"] = request.headers["x-amz-content-sha256"]
        seen["run"] = request.headers["x-policy-desk-run"]
        seen["body"] = request.content
        body = response(input_tokens=100, output_tokens=20)
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = ask("Can I use DeepSeek?", client, "https://example.test/", run="eval:canary")
    assert seen["url"] == "https://example.test/ask"
    assert seen["run"] == "eval:canary"  # so the app marks the request's spans synthetic
    assert seen["hash"] == hashlib.sha256(seen["body"]).hexdigest()
    assert json.loads(seen["body"]) == {"question": "Can I use DeepSeek?", "provider": "auto"}
    assert result["output"] == "DeepSeek is restricted at UNSW [1]."
    assert result["tokenUsage"]["total"] == 120
    assert result["metadata"]["response"]["declined"] is False


def test_provider_reports_an_error_status_instead_of_scoring_it() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(503, json={"detail": "down"}))
    with httpx.Client(transport=transport) as client:
        result = ask("Anything", client, "http://app")
    assert "HTTP 503" in result["error"] and "output" not in result


def test_grades_are_recorded_as_a_share_of_five_until_a_minimum_is_set() -> None:
    check = scoring.check_grade("groundedness", 4.0, "mostly supported")
    assert check.passed and check.score == pytest.approx(0.8)
    assert check.reason == "4 of 5: mostly supported"
    low = scoring.check_grade("relevance", 2.0, "off topic", minimum=3.0)
    assert not low.passed and "minimum 3" in low.reason


def test_masking_is_checked_only_for_personal_details_cases() -> None:
    assert scoring.check_masking(ANSWERABLE, response()) is None
    missed = scoring.check_masking(PERSONAL, response(masked=[]))
    assert missed is not None and not missed.passed
    done = scoring.check_masking(PERSONAL, response(masked=["phone number"]))
    assert done is not None and done.passed and "phone number" in done.reason


def test_toxicity_says_when_bedrock_answered_unrated() -> None:
    check = scoring.check_toxicity(response(answer_safety=None, provider="bedrock"))
    assert check.passed and "not rated: Amazon Bedrock" in check.reason
