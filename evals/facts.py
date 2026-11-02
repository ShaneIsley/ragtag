"""The facts check: a judge model gives each expected fact a verdict against the answer.

The judge prompt is prompts/facts-check.v1.yaml; it runs on the app's own chat
deployment through app.answer.AzureChat. A verdict is "stated", "missing" or
"contradicted". The Azure AI Evaluation SDK's completeness grader was tried first
and rejected: it scored an answer that contradicted a case's main fact 4 out of 5.
"""

import json
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ValidationError

from app.prompts import load_prompt

if TYPE_CHECKING:
    from app.answer import Chat

FACTS_PROMPT = "facts-check.v1"
Verdict = Literal["stated", "missing", "contradicted"]


class FactVerdict(BaseModel):
    n: int
    verdict: Verdict
    why: str = ""


def numbered(facts: list[str]) -> str:
    return "\n".join(f"{i}. {fact}" for i, fact in enumerate(facts, start=1))


def parse(text: str, count: int) -> list[FactVerdict]:
    """The judge's verdicts, one per fact in order. Raises ValueError if the reply
    isn't JSON in the expected shape or doesn't cover every fact."""
    body = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
        items = json.loads(body)["facts"]
        verdicts = {v.n: v for v in (FactVerdict.model_validate(i) for i in items)}
    except (json.JSONDecodeError, KeyError, TypeError, ValidationError) as e:
        raise ValueError(f"The facts judge's reply wasn't usable: {e}") from e
    missing = [n for n in range(1, count + 1) if n not in verdicts]
    if missing:
        raise ValueError(f"The facts judge gave no verdict for facts {missing}")
    return [verdicts[n] for n in range(1, count + 1)]


def judge(facts: list[str], answer: str, chat: "Chat") -> list[FactVerdict]:
    """Asks the judge, and once more if its first reply isn't usable."""
    prompt = load_prompt(FACTS_PROMPT)
    user = prompt.user.format(facts=numbered(facts), answer=answer)
    try:
        return parse(chat.complete(prompt.system, user).text, len(facts))
    except ValueError:
        return parse(chat.complete(prompt.system, user).text, len(facts))
