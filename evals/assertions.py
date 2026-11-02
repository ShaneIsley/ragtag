"""promptfoo assertions: each one scores a response with evals/scoring.py.

promptfoo calls these as `file://assertions.py:<name>` with the provider's output and
a context holding the test's vars, the assertion's config and the provider metadata,
where evals/provider.py puts the app's full response.

promptfoo starts a new Python process for every assertion, so each one imports this
module afresh: about 2.7 s with the graders' SDK and Presidio, which the rule checks
don't need. Only the three judged checks import them, inside the function.
"""

from functools import cache
from typing import Any

from app.settings import get_settings
from evals import scoring
from evals.golden import Case, load

Result = dict[str, Any]


@cache
def _cases() -> dict[str, Case]:
    return {c.id: c for c in load().cases}


def _inputs(context: dict[str, Any]) -> tuple[Case, dict[str, Any], dict[str, Any]]:
    case = _cases()[context["vars"]["case_id"]]
    metadata = context.get("metadata") or {}
    return case, metadata.get("response") or {}, context.get("config") or {}


def _result(check: scoring.Check | None) -> Result:
    if check is None:
        return {"pass": True, "score": 1.0, "reason": "not applicable to this case"}
    return {"pass": check.passed, "score": check.score, "reason": check.reason}


def outcome(output: str, context: dict[str, Any]) -> Result:
    case, response, _ = _inputs(context)
    return _result(scoring.check_outcome(case, response))


def forbidden_text(output: str, context: dict[str, Any]) -> Result:
    case, response, _ = _inputs(context)
    return _result(scoring.check_forbidden_text(case, response))


def recall(output: str, context: dict[str, Any]) -> Result:
    case, response, config = _inputs(context)
    return _result(scoring.check_recall(case, response, config.get("minimum")))


def cites_expected(output: str, context: dict[str, Any]) -> Result:
    case, response, config = _inputs(context)
    return _result(scoring.check_cites_expected(case, response, config.get("minimum")))


def masking(output: str, context: dict[str, Any]) -> Result:
    case, response, _ = _inputs(context)
    return _result(scoring.check_masking(case, response))


def toxicity(output: str, context: dict[str, Any]) -> Result:
    _, response, _ = _inputs(context)
    return _result(scoring.check_toxicity(response))


def check_facts(output: str, context: dict[str, Any]) -> Result:
    """Calls the judge model (prompts/facts-check.v1.yaml) once per answered case."""
    from app.answer import AzureChat
    from evals import facts

    case, response, _ = _inputs(context)
    if not scoring.needs_facts_check(case, response):
        return _result(None)
    try:
        verdicts = facts.judge(
            case.expect.facts, str(response.get("answer", "")), AzureChat(get_settings())
        )
    except ValueError as e:
        return {"pass": False, "score": 0.0, "reason": str(e)}
    return _result(scoring.check_facts(case, response, verdicts))


def _graded(context: dict[str, Any], name: str) -> Result:
    from evals import graders

    case, response, config = _inputs(context)
    if response.get("declined") or case.expect.outcome != "answer":
        return _result(None)
    settings, question, answer = get_settings(), case.last_question, str(response["answer"])
    if name == "groundedness":
        grade = graders.groundedness(settings, question, answer, graders.cited_context(response))
    else:
        grade = graders.relevance(settings, question, answer)
    return _result(scoring.check_grade(name, grade.score, grade.reason, config.get("minimum")))


def groundedness(output: str, context: dict[str, Any]) -> Result:
    """Calls the SDK's groundedness grader once per answered case."""
    return _graded(context, "groundedness")


def relevance(output: str, context: dict[str, Any]) -> Result:
    """Calls the SDK's relevance grader once per answered case."""
    return _graded(context, "relevance")


def latency(output: str, context: dict[str, Any]) -> Result:
    _, _, config = _inputs(context)
    latency_ms = float((context.get("metadata") or {}).get("latency_ms", 0.0))
    return _result(scoring.check_latency(latency_ms, config.get("maximum_ms")))
