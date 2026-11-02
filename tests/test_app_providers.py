from typing import Any

import boto3
import httpx
import openai
import pytest
from botocore.stub import Stubber
from fastapi.testclient import TestClient

from app import main
from app.answer import Completion, ContentFiltered, Effort
from app.main import app, get_chat, get_retriever
from app.providers import BedrockChat, DownChat, FailoverChat, model_label
from app.settings import Settings
from tests.fakes import FakeChat, FakeRetriever, passage

REQUEST = httpx.Request("POST", "https://example.openai.azure.com/")


def status(code: int) -> Any:
    """A response for openai's errors, which are typed against its own httpx build."""
    return httpx.Response(code, request=REQUEST)


def settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_bedrock_chat_uses_the_converse_api_and_reports_itself() -> None:
    client = boto3.client(
        "bedrock-runtime",
        region_name="ap-southeast-2",
        aws_access_key_id="x",
        aws_secret_access_key="y",
    )
    stub = Stubber(client)
    stub.add_response(
        "converse",
        {
            "output": {"message": {"role": "assistant", "content": [{"text": "Yes [1]."}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 900, "outputTokens": 40, "totalTokens": 940},
            "metrics": {"latencyMs": 800},
        },
        {
            "modelId": "au.anthropic.claude-haiku-4-5-20251001-v1:0",
            "system": [{"text": "rules"}],
            "messages": [{"role": "user", "content": [{"text": "question"}]}],
            "inferenceConfig": {"maxTokens": 2000},
        },
    )
    with stub:
        result = BedrockChat(settings(), client=client).complete("rules", "question")
    assert (result.text, result.provider) == ("Yes [1].", "bedrock")
    assert (result.input_tokens, result.output_tokens) == (900, 40)
    assert model_label(result.model) == "Claude Haiku 4.5"


class Failing:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def complete(self, system: str, user: str, *, effort: Effort = "low") -> Completion:
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        openai.APIConnectionError(request=REQUEST),  # type: ignore[arg-type]
        openai.APITimeoutError(request=REQUEST),  # type: ignore[arg-type]
        openai.RateLimitError("busy", response=status(429), body=None),
        openai.InternalServerError("down", response=status(500), body=None),
    ],
)
def test_azure_failures_fail_over_to_bedrock(error: Exception) -> None:
    backup = FakeChat("From Bedrock [1].", provider="bedrock")
    result = FailoverChat(Failing(error), backup).complete("system", "user")
    assert result.provider == "bedrock" and result.text == "From Bedrock [1]."
    assert result.failover_reason and "Azure OpenAI didn't respond" in result.failover_reason


@pytest.mark.parametrize(
    "error",
    [ContentFiltered(None), openai.BadRequestError("bad", response=status(400), body=None)],
)
def test_a_blocked_or_bad_request_never_fails_over(error: Exception) -> None:
    backup = FakeChat("unused", provider="bedrock")
    with pytest.raises(type(error)):
        FailoverChat(Failing(error), backup).complete("system", "user")
    assert backup.calls == [] and backup.rewrite_calls == []


def test_the_drill_makes_azure_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(openai.APIConnectionError):
        DownChat().complete("system", "user")
    main.get_azure_chat.cache_clear()
    monkeypatch.setattr(main, "get_settings", lambda: settings(failover_drill=True))
    try:
        assert isinstance(main.get_azure_chat(), DownChat)
    finally:
        main.get_azure_chat.cache_clear()


def test_the_api_answers_with_the_model_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    bedrock = FakeChat("From Bedrock [1].", provider="bedrock")
    monkeypatch.setattr(main, "get_bedrock_chat", lambda: bedrock)
    app.dependency_overrides[get_retriever] = lambda: FakeRetriever([passage(1)])
    app.dependency_overrides[get_chat] = lambda: FakeChat("From Azure [1].")
    question = "Can I use Copilot?"
    try:
        client = TestClient(app)
        usual = client.post("/ask", json={"question": question}).json()
        other = client.post("/ask", json={"question": question, "provider": "bedrock"}).json()
        wrong = client.post("/ask", json={"question": question, "provider": "gpt"})
        page = client.get("/").text
    finally:
        app.dependency_overrides.clear()
    assert (usual["provider"], usual["answered_by"]) == ("azure", "fake-model on Azure OpenAI")
    assert (other["provider"], other["answer"]) == ("bedrock", "From Bedrock [1].")
    assert other["answered_by"] == "fake-model on Amazon Bedrock"
    assert wrong.status_code == 422
    assert 'id="compare"' in page and 'id="provenance"' in page
    assert "Claude Haiku 4.5 on Amazon Bedrock answers instead" in page
