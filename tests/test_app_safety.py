from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest
from fastapi.testclient import TestClient
from openai.types.chat import ChatCompletion

from app.answer import AzureChat, ContentFiltered, Safety, answer, safety_from
from app.main import app, get_chat, get_retriever
from app.settings import Settings
from tests.fakes import FakeChat, FakeRetriever, passage

# The shape Azure OpenAI returned for a safe question on 31 Oct 2026.
SAFE_ANSWER = {
    "hate": {"filtered": False, "severity": "safe"},
    "protected_material_code": {"detected": False, "filtered": False},
    "protected_material_text": {"detected": False, "filtered": False},
    "self_harm": {"filtered": False, "severity": "safe"},
    "sexual": {"filtered": False, "severity": "safe"},
    "violence": {"filtered": False, "severity": "safe"},
}
FLAGGED_PROMPT = {
    "hate": {"filtered": False, "severity": "safe"},
    "jailbreak": {"detected": True, "filtered": False},
    "self_harm": {"filtered": False, "severity": "safe"},
    "sexual": {"filtered": False, "severity": "safe"},
    "violence": {"filtered": False, "severity": "low"},
}


def test_safety_reads_severities_and_the_jailbreak_check() -> None:
    safety = safety_from(FLAGGED_PROMPT)
    assert safety is not None
    assert safety.severities["violence"] == "low" and safety.jailbreak is True
    assert not safety.filtered
    assert safety.flags == ["Violence: low", "Possible attempt to change the service's rules"]


def test_safe_text_has_no_flags_and_a_filter_error_means_no_rating() -> None:
    safety = safety_from(SAFE_ANSWER)
    assert safety is not None and safety.flags == [] and safety.jailbreak is None
    assert safety_from({"error": {"code": "content_filter_error"}}) is None
    assert safety_from(None) is None


def azure_chat(create: Any) -> AzureChat:
    chat = AzureChat(
        Settings(
            _env_file=None,
            azure_openai_endpoint="https://example.openai.azure.com/",
            azure_openai_api_key="key",
            azure_openai_chat_deployment="chat",
        )
    )
    chat._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))  # type: ignore[assignment]
    return chat


def completion(finish_reason: str = "stop", content: str = "An answer [1].") -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "c1",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-5-mini",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": {"role": "assistant", "content": content},
                    "content_filter_results": SAFE_ANSWER,
                }
            ],
            "prompt_filter_results": [
                {"prompt_index": 0, "content_filter_results": FLAGGED_PROMPT}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )


def test_azure_chat_returns_the_prompt_and_answer_ratings() -> None:
    result = azure_chat(lambda **_: completion()).complete("system", "user")
    assert result.prompt_safety is not None and result.prompt_safety.jailbreak is True
    assert result.safety is not None and result.safety.flags == []


def test_azure_chat_marks_a_filtered_answer() -> None:
    result = azure_chat(lambda **_: completion("content_filter", "")).complete("s", "u")
    assert result.safety is not None and result.safety.filtered


def test_azure_chat_raises_content_filtered_for_a_blocked_prompt() -> None:
    body = {
        "code": "content_filter",
        "innererror": {
            "code": "ResponsibleAIPolicyViolation",
            "content_filter_result": FLAGGED_PROMPT
            | {"violence": {"filtered": True, "severity": "high"}},
        },
    }

    def create(**_: Any) -> None:
        request = httpx.Request("POST", "https://example.openai.azure.com/")
        response = httpx.Response(400, request=request)
        # openai types its errors against its own httpx build.
        raise openai.BadRequestError("blocked", response=response, body=body)  # type: ignore[arg-type]

    with pytest.raises(ContentFiltered) as caught:
        azure_chat(create).complete("system", "user")
    assert caught.value.safety is not None
    assert caught.value.safety.severities["violence"] == "high"


def test_a_blocked_question_is_declined_before_search() -> None:
    retriever = FakeRetriever([passage(1)])
    chat = FakeChat("unused", block="rewrite", question_safety=safety_from(FLAGGED_PROMPT))
    result = answer("A question Azure blocks", retriever, chat)
    assert result.declined and result.decline_reason == "question_filtered"
    assert retriever.questions == [] and chat.calls == []
    assert result.question_safety is not None and result.question_safety.filtered


def test_a_blocked_answer_is_declined() -> None:
    chat = FakeChat("", answer_safety=Safety({"violence": "medium"}, filtered=True))
    result = answer("Who approves AI?", FakeRetriever([passage(1)]), chat)
    assert result.declined and result.decline_reason == "answer_filtered"
    assert result.answer_safety is not None and result.answer_safety.filtered


def test_the_api_returns_the_ratings_and_the_page_can_show_them() -> None:
    chat = FakeChat(
        "The Board approves it [1].",
        question_safety=safety_from(FLAGGED_PROMPT),
        answer_safety=safety_from(SAFE_ANSWER),
    )
    app.dependency_overrides[get_retriever] = lambda: FakeRetriever([passage(1)])
    app.dependency_overrides[get_chat] = lambda: chat
    try:
        client = TestClient(app)
        body = client.post("/ask", json={"question": "Who approves AI?"}).json()
        page = client.get("/").text
    finally:
        app.dependency_overrides.clear()
    assert body["question_safety"]["violence"] == "low"
    assert body["question_safety"]["jailbreak"] is True
    assert body["question_safety"]["flags"][0] == "Violence: low"
    assert body["answer_safety"]["flags"] == []
    assert 'id="safety"' in page and "Content safety rating for this question" in page
