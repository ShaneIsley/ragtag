import openai
import pytest

from app.answer import (
    DECLINE_MESSAGES,
    MIN_SIMILARITY,
    Completion,
    Effort,
    answer,
    check_citations,
    cited_numbers,
    format_passages,
    rewrite_query,
)
from app.prompts import load_prompt
from app.search import diversify, vector_similarity
from tests.fakes import FakeChat, FakeRetriever, passage

PASSAGES = [passage(n) for n in range(1, 4)]


def test_answer_prompt_loads_with_its_placeholders() -> None:
    prompt = load_prompt("answer.v1")
    assert prompt.version == "answer.v1"
    assert "{passages}" in prompt.user and "{question}" in prompt.user
    assert "NO_ANSWER" in prompt.system


def test_cited_answer() -> None:
    chat = FakeChat("The agency's accountable official approves it [2]. They also log it [1][2].")
    result = answer("Who approves high-risk AI?", FakeRetriever(PASSAGES), chat)
    assert not result.declined
    assert [c.number for c in result.citations] == [2, 1]
    assert result.citations[0].passage == PASSAGES[1]
    assert (result.model, result.prompt_version) == ("fake-model", "answer.v4")
    _system, user = chat.calls[0]
    assert "[3] Policy for the responsible use" in user
    assert user.rstrip().endswith("Question: Who approves high-risk AI?")


def test_declines_without_calling_the_model_when_nothing_is_similar() -> None:
    weak = [passage(1, similarity=MIN_SIMILARITY - 0.01), passage(2, similarity=None)]
    chat = FakeChat("should not be called [1]")
    result = answer("How do I bake bread?", FakeRetriever(weak), chat)
    assert result.declined and result.decline_reason == "no_match"
    assert result.text == DECLINE_MESSAGES["no_match"]
    assert chat.calls == []


def test_declines_when_search_finds_nothing() -> None:
    result = answer("anything", FakeRetriever([]), FakeChat("x [1]"))
    assert result.decline_reason == "no_match"


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        ("NO_ANSWER", "not_in_sources"),
        ("NO_ANSWER\n", "not_in_sources"),
        ("Agencies must do this.", "no_citations"),
        ("Agencies must do this [4].", "bad_citation"),
        ("Agencies must do this [0].", "bad_citation"),
    ],
)
def test_rejects_answers_without_valid_citations(reply: str, reason: str) -> None:
    result = answer("Who approves?", FakeRetriever(PASSAGES), FakeChat(reply))
    assert result.declined
    assert result.decline_reason == reason
    assert result.citations == []
    assert result.model == "fake-model"  # the model was called, and that is recorded


def test_cited_numbers_keep_first_order() -> None:
    assert cited_numbers("a [3] b [1] c [3][2]") == [3, 1, 2]


def test_check_citations() -> None:
    assert check_citations("ok [1] [3]", PASSAGES) is None
    assert check_citations("none", PASSAGES) == "no_citations"


def test_format_passages_numbers_from_one() -> None:
    text = format_passages(PASSAGES[:2])
    assert text.startswith("[1] Policy for the responsible use of AI in government > Requirements")
    assert "\n\n[2] " in text


def test_vector_similarity_from_debug_info() -> None:
    def result(*vectors: object) -> dict[str, object]:
        return {"@search.document_debug_info": {"vectors": {"subscores": {"vectors": vectors}}}}

    assert vector_similarity(result({"content_vector": {"vectorSimilarity": 0.64}})) == 0.64
    # A passage only the keyword search found has no vector score.
    assert vector_similarity(result({"content_vector": None})) is None
    assert vector_similarity({}) is None


def test_rewrite_is_searched_with_the_question() -> None:
    retriever = FakeRetriever(PASSAGES)
    chat = FakeChat("UNSW requires approval [1].", rewrite="approval of new AI tools at UNSW")
    result = answer("Who approves high-risk AI?", retriever, chat)
    assert retriever.questions == ["Who approves high-risk AI?"]
    assert retriever.rewrites == ["approval of new AI tools at UNSW"]
    assert result.search_query == "approval of new AI tools at UNSW"
    assert result.rewrite_version == "rewrite.v2"
    assert (result.input_tokens, result.output_tokens) == (130, 30)  # both calls


def test_rewrite_output_is_one_clean_line() -> None:
    chat = FakeChat("x", rewrite='Question: "AI tools" OR (approval)?\nI broadened it.')
    query, _ = rewrite_query("Who approves AI?", chat, load_prompt("rewrite.v2"))
    assert query == "AI tools approval ?"


def test_failed_rewrite_falls_back_to_the_question() -> None:
    class RewriteDown(FakeChat):
        def complete(self, system: str, user: str, *, effort: Effort = "low") -> Completion:
            if system == load_prompt("rewrite.v2").system:
                raise openai.APIConnectionError(request=None)  # type: ignore[arg-type]
            return super().complete(system, user, effort=effort)

    retriever = FakeRetriever(PASSAGES)
    result = answer("Who approves?", retriever, RewriteDown("The CIO's delegate [1]."))
    assert retriever.rewrites == [None]
    assert not result.declined and result.search_query is None


def test_similarity_comes_from_the_questions_vector_not_the_rewrites() -> None:
    def scored(*sims: float | None) -> dict[str, object]:
        vectors = [
            {"content_vector": {"vectorSimilarity": s} if s is not None else None} for s in sims
        ]
        return {"@search.document_debug_info": {"vectors": {"subscores": {"vectors": vectors}}}}

    assert vector_similarity(scored(0.3, 0.9)) == 0.3
    assert vector_similarity(scored(None, 0.9)) is None


def test_diversify_caps_each_source_and_keeps_rank_order() -> None:
    ranked = [passage(n, source_id="a") for n in range(1, 7)] + [passage(7, source_id="b")]
    kept = diversify(ranked, top=5, per_source=4)
    assert [p.content[:9] for p in kept] == [f"Passage {n}" for n in (1, 2, 3, 4, 7)]
