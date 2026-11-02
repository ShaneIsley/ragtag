import asyncio
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from botocore.credentials import Credentials
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import main, telemetry
from app.answer import Safety
from app.embedding import AzureEmbedder
from app.main import app, get_chat, get_retriever
from app.settings import Settings
from tests.fakes import FakeChat, FakeRetriever, passage

QUESTION = "Hi, I'm Jordan Example, z0000000. Can I use Copilot to plan my essay?"
ANSWER = "Check your course rules first [1]."


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    telemetry.add_processor(Settings(_env_file=None), SimpleSpanProcessor(exporter))
    app.dependency_overrides[get_retriever] = lambda: FakeRetriever(
        [passage(1), passage(2), passage(3, source_id="unsw-copilot")]
    )
    app.dependency_overrides[get_chat] = lambda: FakeChat(ANSWER)
    yield exporter
    app.dependency_overrides.clear()


def by_name(finished: tuple[ReadableSpan, ...]) -> dict[str, ReadableSpan]:
    return {s.name: s for s in finished}


def test_an_answer_is_one_trace_with_a_span_per_step(spans: InMemorySpanExporter) -> None:
    TestClient(app).post("/ask", json={"question": QUESTION})
    named = by_name(spans.get_finished_spans())
    steps = ("POST /ask", "answer", "mask", "rewrite", "search", "generate", "check_citations")
    for step in steps:
        assert step in named, step
    assert len({s.context.trace_id for s in named.values()}) == 1
    answered: Any = named["answer"].attributes
    assert answered["app.declined"] is False and answered["app.provider"] == "azure"
    assert named["search"].attributes["app.passage_ids"] == (  # type: ignore[index]
        passage(1).id,
        passage(2).id,
        passage(3, source_id="unsw-copilot").id,
    )
    assert named["search"].attributes["app.source_ids"] == (  # type: ignore[index]
        "dta-ai-policy-v2",
        "unsw-copilot",
    )
    assert answered["app.cited_sources"] == ("dta-ai-policy-v2",)
    assert named["mask"].attributes["app.masked"] == ("name", "zID")  # type: ignore[index]


def test_the_evaluation_s_requests_are_marked_synthetic(spans: InMemorySpanExporter) -> None:
    client = TestClient(app)
    client.post("/ask", json={"question": QUESTION}, headers={"x-policy-desk-run": "eval:canary"})
    named = by_name(spans.get_finished_spans())
    for step in ("answer", "mask", "search", "check_citations"):
        assert named[step].attributes["app.synthetic"] == "eval:canary", step  # type: ignore[index]
    spans.clear()
    client.post("/ask", json={"question": QUESTION})
    assert not any("app.synthetic" in (s.attributes or {}) for s in spans.get_finished_spans())


def test_a_run_label_keeps_only_safe_characters(spans: InMemorySpanExporter) -> None:
    TestClient(app).post(
        "/ask", json={"question": QUESTION}, headers={"x-policy-desk-run": "eval canary<b>"}
    )
    named = by_name(spans.get_finished_spans())
    assert named["answer"].attributes["app.synthetic"] == "evalcanaryb"  # type: ignore[index]


def test_model_calls_carry_the_genai_attributes(spans: InMemorySpanExporter) -> None:
    TestClient(app).post("/ask", json={"question": "Can I use Copilot?"})
    calls = [s for s in spans.get_finished_spans() if s.name == "chat fake-model"]
    assert len(calls) == 2  # the rewrite and the answer
    attributes: Any = calls[-1].attributes
    assert attributes["gen_ai.operation.name"] == "chat"
    assert attributes["gen_ai.provider.name"] == "azure.ai.openai"
    assert attributes["gen_ai.usage.input_tokens"] == 100
    assert attributes["gen_ai.usage.output_tokens"] == 20


def test_no_span_holds_the_question_the_answer_or_what_was_masked(
    spans: InMemorySpanExporter,
) -> None:
    TestClient(app).post("/ask", json={"question": QUESTION})
    finished = spans.get_finished_spans()
    values = [str(v) for s in finished for v in (s.attributes or {}).values()]
    for secret in (QUESTION, ANSWER, "Jordan Example", "z0000000", "plan my essay"):
        assert not any(secret in v for v in values), secret
    keys = {k for s in finished for k in (s.attributes or {})}
    assert not keys & {"client.address", "net.peer.ip", "http.client_ip", "user_agent.original"}


def test_the_trace_id_is_returned_only_when_traces_are_exported(
    spans: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(app)
    assert client.post("/ask", json={"question": "Can I use Copilot?"}).json()["trace_id"] is None
    monkeypatch.setattr(
        main, "get_settings", lambda: Settings(_env_file=None, otel_traces_exporter="otlp")
    )
    trace_id = client.post("/ask", json={"question": "Can I use Copilot?"}).json()["trace_id"]
    assert trace_id and len(trace_id) == 32
    assert trace_id in {format(s.context.trace_id, "032x") for s in spans.get_finished_spans()}


def test_the_answer_span_records_what_the_safety_ratings_found(
    spans: InMemorySpanExporter,
) -> None:
    app.dependency_overrides[get_chat] = lambda: FakeChat(
        ANSWER,
        question_safety=Safety({"hate": "safe", "violence": "low"}, jailbreak=True),
        answer_safety=Safety({"hate": "safe"}),
    )
    TestClient(app).post("/ask", json={"question": "Can I use Copilot?"})
    answered: Any = by_name(spans.get_finished_spans())["answer"].attributes
    assert answered["app.question_rated"] is True
    assert answered["app.question_flags"] == ("violence:low", "jailbreak")
    assert answered["app.answer_flags"] == ()


def test_an_embedding_call_gets_a_genai_span(spans: InMemorySpanExporter) -> None:
    settings = Settings(
        _env_file=None,
        azure_openai_endpoint="https://example.openai.azure.com",
        azure_openai_api_key="key",  # type: ignore[arg-type]
        azure_openai_embedding_deployment="embed",
    )
    embedder = AzureEmbedder(settings)
    response = SimpleNamespace(
        data=[SimpleNamespace(index=0, embedding=[0.1, 0.2])],
        model="text-embedding-3-small",
        usage=SimpleNamespace(prompt_tokens=7),
    )
    embedder._client = SimpleNamespace(  # type: ignore[assignment]
        embeddings=SimpleNamespace(create=lambda **_: response)
    )
    assert embedder.embed(["Can I use Copilot?"]) == [[0.1, 0.2]]
    span = by_name(spans.get_finished_spans())["embeddings embed"]
    attributes: Any = span.attributes
    assert attributes["gen_ai.operation.name"] == "embeddings"
    assert attributes["gen_ai.usage.input_tokens"] == 7


def test_the_first_request_a_process_serves_is_marked_as_a_cold_start(
    spans: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(telemetry, "_served_a_request", False)
    client = TestClient(app)
    client.get("/health")
    client.get("/health")
    first, second = [s for s in spans.get_finished_spans() if s.name == "GET /health"]
    assert (first.attributes or {}).get("faas.coldstart") is True
    assert "faas.coldstart" not in (second.attributes or {})


def test_the_response_ends_only_after_its_spans_are_sent(
    spans: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    # By the time spans are flushed, FastAPI's request span has ended too.
    ended_at_flush: list[set[str]] = []
    monkeypatch.setattr(
        telemetry,
        "flush",
        lambda: ended_at_flush.append({s.name for s in spans.get_finished_spans()}),
    )
    response = TestClient(main.server).post("/ask", json={"question": "Can I use Copilot?"})
    assert response.status_code == 200
    assert {"POST /ask", "answer"} <= ended_at_flush[-1]


def test_flush_spans_holds_back_only_the_end_of_the_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(telemetry, "flush", lambda: events.append("flush"))

    async def asgi_app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"part", "more_body": True})
        await send({"type": "http.response.body", "body": b"end"})

    async def send(message: Any) -> None:
        events.append(f"{message['type']} {message.get('body', b'').decode()}".strip())

    asyncio.run(telemetry.FlushSpans(asgi_app)({"type": "http"}, None, send))  # type: ignore[arg-type]
    assert events == [
        "http.response.start",
        "http.response.body part",
        "flush",
        "http.response.body end",
    ]


def test_only_an_xray_endpoint_is_signed() -> None:
    assert telemetry.xray_region("https://xray.ap-southeast-2.amazonaws.com/v1/traces") == (
        "ap-southeast-2"
    )
    assert telemetry.xray_region("http://jaeger:4318/v1/traces") is None
    assert telemetry.xray_region("https://xray.ap-southeast-2.amazonaws.com.example.net") is None


def test_exports_to_xray_are_signed_for_the_xray_service() -> None:
    request = requests.Request(
        "POST",
        "https://xray.ap-southeast-2.amazonaws.com/v1/traces",
        data=b"spans",
        headers={"Content-Type": "application/x-protobuf"},
    ).prepare()
    signed = telemetry.SigV4("ap-southeast-2", Credentials("AKIDEXAMPLE", "secret"))(request)
    authorization = signed.headers["Authorization"]
    assert authorization.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/ap-southeast-2/xray/aws4_request" in authorization
    assert "X-Amz-Date" in signed.headers


def test_the_image_keeps_no_request_log() -> None:
    # The request log was the only place a visitor's IP address could be recorded.
    dockerfile = Path(__file__).parents[1] / "Dockerfile.app"
    cmd = next(line for line in dockerfile.read_text().splitlines() if line.startswith("CMD"))
    assert "--no-access-log" in cmd
