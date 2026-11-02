"""Turn a question into a cited answer, or a decline."""

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import openai
from openai import AzureOpenAI

from app import telemetry
from app.masking import Masked, mask_question
from app.prompts import Prompt, load_prompt
from app.search import Passage, Retriever
from app.settings import AZURE_OPENAI_API_VERSION, Settings, require

log = logging.getLogger("app")

# Below this cosine similarity the best passage isn't about the question, so the app
# declines without calling the model. Provisional (31 Oct 2026, UNSW-only index):
# UNSW questions scored 0.46 to 0.82 and off-topic ones 0.09 to 0.41. AI questions that
# UNSW's documents don't cover score 0.48 to 0.55, so the model's NO_ANSWER handles
# those. Phase 2 sets the value from the evaluation baseline.
MIN_SIMILARITY = 0.40
# The prompt versions the app uses (prompts/*.yaml).
ANSWER_PROMPT = "answer.v4"
REWRITE_PROMPT = "rewrite.v2"
NO_ANSWER = "NO_ANSWER"
MAX_QUESTION_CHARS = 500
MAX_REWRITE_CHARS = 300

_CITATION = re.compile(r"\[(\d+)\]")
_SEARCH_SYNTAX = re.compile(r"[\"+|*()~^\\]|\b(?:AND|OR|NOT)\b")

Effort = Literal["minimal", "low", "medium", "high"]
DeclineReason = Literal[
    "no_match",
    "not_in_sources",
    "no_citations",
    "bad_citation",
    "question_filtered",
    "answer_filtered",
]

DECLINE_MESSAGES: dict[DeclineReason, str] = {
    "no_match": (
        "I couldn't find anything about that in the UNSW documents I cover. I answer "
        "questions about how AI may be used at UNSW."
    ),
    "not_in_sources": (
        "The UNSW documents I cover don't answer that. Try asking what UNSW requires, "
        "which AI tools are approved, or how AI may be used in study, teaching or research."
    ),
    "no_citations": (
        "I couldn't produce an answer backed by the policies, so I haven't given one. "
        "Try rephrasing the question."
    ),
    "bad_citation": (
        "I couldn't produce an answer backed by the policies, so I haven't given one. "
        "Try rephrasing the question."
    ),
    "question_filtered": (
        "Azure's content safety filter blocked this question, so I haven't answered it. "
        "I answer questions about how AI may be used at UNSW."
    ),
    "answer_filtered": (
        "Azure's content safety filter blocked the answer, so I haven't given one. "
        "Try rephrasing the question."
    ),
}

# Azure OpenAI rates every prompt and completion in these four categories, as safe, low,
# medium or high, and blocks medium and high by default. Prompts also get a jailbreak
# check (Prompt Shields).
HARM_CATEGORIES = ("hate", "sexual", "violence", "self_harm")
CATEGORY_LABELS = {
    "hate": "Hate",
    "sexual": "Sexual",
    "violence": "Violence",
    "self_harm": "Self-harm",
}


@dataclass(frozen=True)
class Safety:
    """Azure OpenAI's content ratings for one prompt or completion."""

    severities: Mapping[str, str] = field(default_factory=dict)
    jailbreak: bool | None = None
    filtered: bool = False

    @property
    def flags(self) -> list[str]:
        """What the ratings found, in words for the page: empty when all is safe."""
        found = [
            f"{CATEGORY_LABELS[c]}: {self.severities[c]}"
            for c in HARM_CATEGORIES
            if self.severities.get(c, "safe") != "safe"
        ]
        if self.jailbreak:
            found.append("Possible attempt to change the service's rules")
        return found


def safety_from(results: Mapping[str, Any] | None) -> Safety | None:
    """Ratings from a `content_filter_results` object, or None if Azure didn't rate
    the text (the filter reports an error when it couldn't run)."""
    if not results or "error" in results:
        return None
    severities = {
        c: str(results[c]["severity"])
        for c in HARM_CATEGORIES
        if isinstance(results.get(c), Mapping) and "severity" in results[c]
    }
    jailbreak = results.get("jailbreak")
    filtered = any(isinstance(v, Mapping) and v.get("filtered") for v in results.values())
    return Safety(
        severities=severities,
        jailbreak=bool(jailbreak.get("detected")) if isinstance(jailbreak, Mapping) else None,
        filtered=filtered,
    )


class ContentFiltered(Exception):
    """Azure blocked the prompt. Carries the prompt's ratings, where Azure gave them."""

    def __init__(self, safety: Safety | None) -> None:
        super().__init__("The prompt was blocked by the content filter")
        self.safety = safety


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    # Azure's ratings of the prompt and of this completion. None from fakes, from
    # Bedrock, or when the filter didn't run.
    prompt_safety: Safety | None = None
    safety: Safety | None = None
    # Which service answered ("azure" or "bedrock"), and why it wasn't the first
    # choice, if it wasn't.
    provider: str = ""
    failover_reason: str | None = None


class Chat(Protocol):
    def complete(self, system: str, user: str, *, effort: Effort = "low") -> Completion: ...


class AzureChat:
    def __init__(self, settings: Settings) -> None:
        require(
            settings,
            "azure_openai_endpoint",
            "azure_openai_api_key",
            "azure_openai_chat_deployment",
        )
        self._client = AzureOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key.get_secret_value(),
            api_version=AZURE_OPENAI_API_VERSION,
            timeout=60.0,
            max_retries=2,
        )
        self._deployment = settings.azure_openai_chat_deployment

    def complete(self, system: str, user: str, *, effort: Effort = "low") -> Completion:
        try:
            response = self._client.chat.completions.create(
                model=self._deployment,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_completion_tokens=2000,
                reasoning_effort=effort,
            )
        except openai.BadRequestError as e:
            if e.code != "content_filter":
                raise
            inner = e.body.get("innererror", {}) if isinstance(e.body, dict) else {}
            raise ContentFiltered(safety_from(inner.get("content_filter_result"))) from e
        # The ratings are Azure's additions to the OpenAI response, so they're extra fields.
        data = response.model_dump()
        prompts = data.get("prompt_filter_results") or [{}]
        choice = response.choices[0]
        safety = safety_from(data["choices"][0].get("content_filter_results"))
        if choice.finish_reason == "content_filter":
            safety = Safety(safety.severities if safety else {}, filtered=True)
        usage = response.usage
        return Completion(
            text=choice.message.content or "",
            model=response.model,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            prompt_safety=safety_from(prompts[0].get("content_filter_results")),
            safety=safety,
            provider="azure",
        )


@dataclass(frozen=True)
class Citation:
    number: int
    passage: Passage


@dataclass(frozen=True)
class Answer:
    question: str
    text: str
    declined: bool
    prompt_version: str
    # The broader query searched alongside the question, and the prompt that wrote it.
    search_query: str | None = None
    rewrite_version: str | None = None
    decline_reason: DeclineReason | None = None
    citations: list[Citation] = field(default_factory=list)
    passages: list[Passage] = field(default_factory=list)
    top_similarity: float | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    # Azure's content ratings of the question and of the model's answer.
    question_safety: Safety | None = None
    answer_safety: Safety | None = None
    # The question as the models saw it, with personal details masked, and what was
    # removed (such as "name" or "email address").
    sent_question: str = ""
    masked: tuple[str, ...] = ()
    # Which service wrote the answer, and why it wasn't Azure OpenAI, if it wasn't.
    provider: str = ""
    failover_reason: str | None = None


def blocked(safety: Safety | None) -> Safety:
    """The ratings of a prompt Azure blocked, marked as filtered."""
    if safety is None:
        return Safety(filtered=True)
    return Safety(safety.severities, safety.jailbreak, filtered=True)


def top_similarity(passages: list[Passage]) -> float | None:
    scores = [p.similarity for p in passages if p.similarity is not None]
    return max(scores) if scores else None


def format_passages(passages: list[Passage]) -> str:
    return "\n\n".join(
        f"[{n}] {p.title}" + (f" > {p.section}" if p.section else "") + f"\n{p.content}"
        for n, p in enumerate(passages, start=1)
    )


def cited_numbers(text: str) -> list[int]:
    """Passage numbers in the order the answer first cites them."""
    return list(dict.fromkeys(int(n) for n in _CITATION.findall(text)))


def check_citations(text: str, passages: list[Passage]) -> DeclineReason | None:
    numbers = cited_numbers(text)
    if not numbers:
        return "no_citations"
    if any(not 1 <= n <= len(passages) for n in numbers):
        return "bad_citation"
    return None


class TracedChat:
    """A model whose every call gets a GenAI span, named for the model that answered."""

    def __init__(self, chat: Chat) -> None:
        self._chat = chat

    def complete(self, system: str, user: str, *, effort: Effort = "low") -> Completion:
        with telemetry.tracer().start_as_current_span("chat") as span:
            completion = self._chat.complete(system, user, effort=effort)
            span.update_name(f"chat {completion.model}")
            telemetry.set_model_call(
                span,
                provider=completion.provider,
                model=completion.model,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                failover_reason=completion.failover_reason,
            )
            return completion


def rewrite_query(
    question: str, chat: Chat, prompt: Prompt
) -> tuple[str | None, Completion | None]:
    """A broader search query for the question, or None if the rewrite fails. Search
    still runs on the question itself, so a failed rewrite only loses recall."""
    try:
        completion = chat.complete(
            prompt.system, prompt.user.format(question=question), effort="minimal"
        )
    except openai.APIError as e:
        log.warning("Search rewrite failed: %s", type(e).__name__)
        return None, None
    lines = completion.text.strip().splitlines()
    query = lines[0].removeprefix("Question:") if lines else ""
    # Quotes and operators change how keyword search reads the text.
    query = " ".join(_SEARCH_SYNTAX.sub(" ", query).split())
    return (query[:MAX_REWRITE_CHARS] or None), completion


def answer(
    question: str,
    retriever: Retriever,
    chat: Chat,
    prompt: Prompt | None = None,
    rewrite_prompt: Prompt | None = None,
    mask: Callable[[str], Masked] = mask_question,
) -> Answer:
    prompt = prompt or load_prompt(ANSWER_PROMPT)
    rewrite_prompt = rewrite_prompt or load_prompt(REWRITE_PROMPT)
    chat = TracedChat(chat)
    # Nothing below sees the question as typed: only its masked form.
    with telemetry.step("mask") as span:
        masking = mask(question)
        telemetry.set_app(span, masked=masking.removed)
    sent = masking.text
    try:
        with telemetry.step("rewrite", prompt_version=rewrite_prompt.version):
            search_query, rewrite = rewrite_query(sent, chat, rewrite_prompt)
    except ContentFiltered as e:
        # The rewrite call sees the question alone, so a block there is the question's.
        return Answer(
            question=question,
            text=DECLINE_MESSAGES["question_filtered"],
            declined=True,
            prompt_version=prompt.version,
            rewrite_version=rewrite_prompt.version,
            decline_reason="question_filtered",
            question_safety=blocked(e.safety),
            sent_question=sent,
            masked=masking.removed,
        )
    with telemetry.step("search") as span:
        passages = retriever.search(sent, search_query)
        best = top_similarity(passages)
        telemetry.set_app(
            span,
            passage_ids=[p.id for p in passages],
            source_ids=list(dict.fromkeys(p.source_id for p in passages)),
            top_similarity=best,
        )

    def result(text: str, completion: Completion | None, **fields: Any) -> Answer:
        done = [c for c in (rewrite, completion) if c]
        # The rewrite call rates the question on its own; the answer call's prompt also
        # holds the passages, so it's the fallback.
        rated = rewrite or completion
        fields.setdefault("question_safety", rated.prompt_safety if rated else None)
        return Answer(
            question=question,
            text=text,
            prompt_version=prompt.version,
            search_query=search_query,
            rewrite_version=rewrite_prompt.version,
            passages=passages,
            top_similarity=best,
            model=completion.model if completion else None,
            input_tokens=sum(c.input_tokens for c in done),
            output_tokens=sum(c.output_tokens for c in done),
            answer_safety=completion.safety if completion else None,
            sent_question=sent,
            masked=masking.removed,
            provider=completion.provider if completion else "",
            failover_reason=completion.failover_reason if completion else None,
            **fields,
        )

    def decline(
        reason: DeclineReason, completion: Completion | None = None, **fields: Any
    ) -> Answer:
        return result(
            DECLINE_MESSAGES[reason], completion, declined=True, decline_reason=reason, **fields
        )

    if best is None or best < MIN_SIMILARITY:
        return decline("no_match")

    try:
        with telemetry.step("generate", prompt_version=prompt.version):
            completion = chat.complete(
                prompt.system,
                prompt.user.format(passages=format_passages(passages), question=sent),
            )
    except ContentFiltered as e:
        return decline("question_filtered", question_safety=blocked(e.safety))
    if completion.safety and completion.safety.filtered:
        return decline("answer_filtered", completion)
    text = completion.text.strip()
    if text == NO_ANSWER or text.startswith(NO_ANSWER):
        return decline("not_in_sources", completion)
    with telemetry.step("check_citations") as span:
        reason = check_citations(text, passages)
        telemetry.set_app(span, cited=cited_numbers(text), problem=reason)
    if reason:
        return decline(reason, completion)
    citations = [Citation(n, passages[n - 1]) for n in cited_numbers(text)]
    return result(text, completion, declined=False, citations=citations)
