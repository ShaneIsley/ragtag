"""Fake search and model clients, so app tests run offline and free."""

from dataclasses import dataclass, field

from app.answer import REWRITE_PROMPT, Completion, ContentFiltered, Effort, Safety
from app.prompts import load_prompt
from app.search import Passage

REWRITE_SYSTEM = load_prompt(REWRITE_PROMPT).system


def passage(n: int, similarity: float | None = 0.7, source_id: str = "dta-ai-policy-v2") -> Passage:
    return Passage(
        id=f"{source_id}_{n:020d}",
        source_id=source_id,
        title="Policy for the responsible use of AI in government",
        section=f"Requirements > Part {n}",
        url="https://www.digital.gov.au/ai/ai-in-government-policy",
        publisher="DTA",
        content=f"Passage {n}: agencies must have a high-risk use case approved.",
        similarity=similarity,
    )


@dataclass
class FakeRetriever:
    passages: list[Passage]
    questions: list[str] = field(default_factory=list)
    rewrites: list[str | None] = field(default_factory=list)

    def search(self, question: str, rewrite: str | None = None) -> list[Passage]:
        self.questions.append(question)
        self.rewrites.append(rewrite)
        return self.passages


@dataclass
class FakeChat:
    """Answers with `reply`. The search-rewrite call gets `rewrite`, if set, or else the
    question back unchanged."""

    reply: str
    rewrite: str | None = None
    # Content safety: the question's and the answer's ratings, and which call Azure
    # blocks, if any.
    question_safety: Safety | None = None
    answer_safety: Safety | None = None
    block: str | None = None  # "rewrite" or "answer"
    provider: str = "azure"
    calls: list[tuple[str, str]] = field(default_factory=list)
    rewrite_calls: list[str] = field(default_factory=list)

    def complete(self, system: str, user: str, *, effort: Effort = "low") -> Completion:
        if system == REWRITE_SYSTEM:
            self.rewrite_calls.append(user)
            if self.block == "rewrite":
                raise ContentFiltered(self.question_safety)
            text = self.rewrite or user.removeprefix("Question: ").strip()
            return Completion(
                text,
                "fake-model",
                30,
                10,
                prompt_safety=self.question_safety,
                provider=self.provider,
            )
        self.calls.append((system, user))
        if self.block == "answer":
            raise ContentFiltered(self.question_safety)
        return Completion(
            self.reply,
            "fake-model",
            100,
            20,
            prompt_safety=self.question_safety,
            safety=self.answer_safety,
            provider=self.provider,
        )
