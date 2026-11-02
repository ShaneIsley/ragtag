import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.answer import Completion, Effort
from evals import assertions, facts, scoring
from evals.facts import FactVerdict
from evals.golden import load

CASE = next(c for c in load().cases if c.id == "deepseek-restricted")  # three facts


def reply(*verdicts: str) -> str:
    return json.dumps(
        {"facts": [{"n": i, "verdict": v, "why": f"fact {i}"} for i, v in enumerate(verdicts, 1)]}
    )


class JudgeChat:
    def __init__(self, text: str) -> None:
        self.text = text
        self.users: list[str] = []

    def complete(self, system: str, user: str, *, effort: Effort = "low") -> Completion:
        self.users.append(user)
        return Completion(self.text, "judge")


def test_parse_reads_one_verdict_per_fact_in_order() -> None:
    text = "```json\n" + reply("stated", "contradicted", "missing") + "\n```"
    verdicts = facts.parse(text, 3)
    assert [v.verdict for v in verdicts] == ["stated", "contradicted", "missing"]


@pytest.mark.parametrize("text", ["not json", '{"facts": [{"n": 1, "verdict": "maybe"}]}'])
def test_parse_rejects_an_unusable_reply(text: str) -> None:
    with pytest.raises(ValueError):
        facts.parse(text, 1)


def test_parse_rejects_a_reply_that_skips_a_fact() -> None:
    with pytest.raises(ValueError, match=r"facts \[3\]"):
        facts.parse(reply("stated", "stated"), 3)


def test_judge_numbers_the_facts_and_passes_the_answer() -> None:
    chat = JudgeChat(reply("stated", "stated", "stated"))
    verdicts = facts.judge(CASE.expect.facts, "DeepSeek is restricted [1].", chat)
    assert len(verdicts) == 3
    assert "1. DeepSeek is restricted" in chat.users[0]
    assert "DeepSeek is restricted [1]." in chat.users[0]


def verdicts(*kinds: str) -> list[FactVerdict]:
    return [FactVerdict(n=i, verdict=k, why=f"why {i}") for i, k in enumerate(kinds, 1)]


def test_a_contradicted_fact_fails_whatever_else_is_stated() -> None:
    check = scoring.check_facts(CASE, {}, verdicts("stated", "contradicted", "stated"))
    assert not check.passed
    assert check.score == pytest.approx(2 / 3)
    assert "contradicted fact 2" in check.reason


def test_missing_facts_lower_the_score_but_pass_until_the_baseline() -> None:
    check = scoring.check_facts(CASE, {}, verdicts("stated", "missing", "missing"))
    assert check.passed and check.score == pytest.approx(1 / 3)


def test_only_answers_are_checked_against_facts() -> None:
    assert scoring.needs_facts_check(CASE, {"declined": False})
    assert not scoring.needs_facts_check(CASE, {"declined": True})
    off_topic = next(c for c in load().cases if c.id == "capital-of-france")
    assert not scoring.needs_facts_check(off_topic, {"declined": False})


def test_the_assertion_judges_the_answer_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_judge(expected: list[str], answer: str, chat: Any) -> list[FactVerdict]:
        calls.append(answer)
        return verdicts("stated", "contradicted", "missing")

    monkeypatch.setattr(assertions, "get_settings", lambda: None)
    monkeypatch.setattr("app.answer.AzureChat", lambda settings: object())
    monkeypatch.setattr(facts, "judge", fake_judge)
    context = {
        "vars": {"case_id": CASE.id},
        "metadata": {"response": {"answer": "Yes, after approval [1].", "declined": False}},
    }
    result = assertions.check_facts("", context)
    assert calls == ["Yes, after approval [1]."]
    assert result["pass"] is False and "contradicted" in result["reason"]
    declined = {"vars": {"case_id": CASE.id}, "metadata": {"response": {"declined": True}}}
    assert assertions.check_facts("", declined)["reason"] == "not applicable to this case"


def test_the_rule_checks_import_without_the_judges_dependencies() -> None:
    """promptfoo imports assertions.py afresh for every check, so loading the graders' SDK
    and Presidio there cost 2.7 s a check (docs/evaluation.md, The first weekly runs)."""
    heavy = ("azure.ai.evaluation", "presidio_analyzer", "openai")
    code = f"import sys, evals.assertions; print([m for m in {heavy!r} if m in sys.modules])"
    root = Path(__file__).parents[1]
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=root)
    assert out.stdout.strip() == "[]", out.stderr


class FlakyJudge(JudgeChat):
    def __init__(self, first: str, second: str) -> None:
        super().__init__(second)
        self.replies = [first, second]

    def complete(self, system: str, user: str, *, effort: Effort = "low") -> Completion:
        self.users.append(user)
        return Completion(self.replies[len(self.users) - 1], "judge")


def test_judge_asks_again_once_when_the_reply_is_unusable() -> None:
    chat = FlakyJudge("not json", reply("stated", "stated", "stated"))
    assert len(facts.judge(CASE.expect.facts, "An answer.", chat)) == 3
    assert len(chat.users) == 2


def test_graders_skip_declines_and_use_the_cited_passages(monkeypatch: pytest.MonkeyPatch) -> None:
    from evals import graders

    seen: dict[str, str] = {}

    def fake_groundedness(settings: Any, question: str, answer: str, context: str) -> Any:
        seen["context"] = context
        return graders.Grade(5.0, "supported")

    monkeypatch.setattr(assertions, "get_settings", lambda: None)
    monkeypatch.setattr(graders, "groundedness", fake_groundedness)
    response = {
        "answer": "DeepSeek is restricted [1].",
        "declined": False,
        "sources": [{"number": 1, "content": "DeepSeek is restricted based on advice."}],
    }
    context = {"vars": {"case_id": CASE.id}, "metadata": {"response": response}}
    result = assertions.groundedness("", context)
    assert result["pass"] and result["score"] == 1.0
    assert seen["context"] == "[1] DeepSeek is restricted based on advice."
    declined = {"vars": {"case_id": CASE.id}, "metadata": {"response": {"declined": True}}}
    assert assertions.relevance("", declined)["reason"] == "not applicable to this case"
