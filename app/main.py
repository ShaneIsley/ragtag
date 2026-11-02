"""The Policy Desk web service: one page and a JSON endpoint that answers a question."""

import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

import openai
from azure.core.exceptions import AzureError
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, StringConstraints

from app import status, telemetry
from app.answer import MAX_QUESTION_CHARS, AzureChat, Chat, Safety, answer
from app.embedding import AzureEmbedder
from app.providers import PROVIDER_NAMES, BedrockChat, DownChat, FailoverChat, model_label
from app.search import AzureRetriever, Retriever
from app.settings import get_settings

log = logging.getLogger("app")

UNAVAILABLE = "The answer service is unavailable right now. Please try again in a minute."

templates = Jinja2Templates(directory=Path(__file__).with_name("templates"))


# No interactive API docs: they load scripts from a CDN, which the content security
# policy below rules out. The schema is still at /openapi.json.
@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Tracing starts with the app, so the first request's span is recorded too.
    telemetry.configure(get_settings())
    yield


app = FastAPI(title="Policy Desk", docs_url=None, redoc_url=None, lifespan=lifespan)


def security_headers(nonce: str) -> dict[str, str]:
    """Headers on every response. The page's one inline script and style carry the
    per-request nonce; nothing else may run, load or frame the page."""
    return {
        "Content-Security-Policy": (
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'none'"
        ),
        "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cache-Control": "no-store",
    }


@app.middleware("http")
async def mark_cold_start(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    telemetry.mark_cold_start()
    return await call_next(request)


@app.middleware("http")
async def add_security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request.state.csp_nonce = secrets.token_urlsafe(16)
    response = await call_next(request)
    response.headers.update(security_headers(request.state.csp_nonce))
    return response


@lru_cache
def get_retriever() -> Retriever:
    settings = get_settings()
    return AzureRetriever(settings, AzureEmbedder(settings))


@lru_cache
def get_azure_chat() -> Chat:
    settings = get_settings()
    return DownChat() if settings.failover_drill else AzureChat(settings)


@lru_cache
def get_bedrock_chat() -> Chat:
    return BedrockChat(get_settings())


@lru_cache
def get_chat() -> Chat:
    """Azure OpenAI, failing over to Bedrock."""
    return FailoverChat(get_azure_chat(), get_bedrock_chat())


Provider = Literal["auto", "azure", "bedrock"]


def chat_for(provider: Provider, auto: Chat) -> Chat:
    """The model to ask: the usual one with failover, or one named by "Try the other
    model"."""
    if provider == "azure":
        return get_azure_chat()
    if provider == "bedrock":
        return get_bedrock_chat()
    return auto


class AskRequest(BaseModel):
    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUESTION_CHARS)
    ]
    provider: "Provider" = Field(
        "auto", description="auto: Azure OpenAI, failing over to Bedrock; or one of them"
    )


class Source(BaseModel):
    number: int = Field(description="The number the answer cites, as in [1]")
    source_id: str
    title: str
    section: str
    publisher: str
    url: str
    passage_id: str
    content: str


class Rating(BaseModel):
    """Azure OpenAI's content safety ratings for the question or the answer."""

    hate: str | None = Field(description="safe, low, medium or high")
    sexual: str | None
    violence: str | None
    self_harm: str | None
    jailbreak: bool | None = Field(description="The question only: Prompt Shields' check")
    filtered: bool = Field(description="Azure blocked the text")
    flags: list[str] = Field(description="What the ratings found, in words; empty if safe")


def safety_flags(safety: Safety | None) -> list[str] | None:
    """What Azure's ratings found, for a span: categories above safe, as
    "category:severity", and "jailbreak". None when Azure didn't rate the text."""
    if safety is None:
        return None
    found = [f"{c}:{s}" for c, s in safety.severities.items() if s != "safe"]
    return [*found, "jailbreak"] if safety.jailbreak else found


def rating(safety: Safety | None) -> Rating | None:
    if safety is None:
        return None
    return Rating(
        hate=safety.severities.get("hate"),
        sexual=safety.severities.get("sexual"),
        violence=safety.severities.get("violence"),
        self_harm=safety.severities.get("self_harm"),
        jailbreak=safety.jailbreak,
        filtered=safety.filtered,
        flags=safety.flags,
    )


class Retrieved(BaseModel):
    passage_id: str
    source_id: str
    similarity: float | None


class AskResponse(BaseModel):
    question: str
    answer: str
    declined: bool
    decline_reason: str | None
    sources: list[Source]
    retrieved: list[Retrieved] = Field(description="Every passage search returned, in rank order")
    top_similarity: float | None
    search_query: str | None = Field(description="The broader query searched with the question")
    prompt_version: str
    rewrite_version: str | None
    model: str | None
    input_tokens: int
    output_tokens: int
    provider: str = Field(description="The service that wrote the answer: azure or bedrock")
    answered_by: str = Field(description="The model and service, as the page shows them")
    failover_reason: str | None = Field(description="Why Bedrock answered, after a failover")
    sent_question: str = Field(description="The question as the models saw it, masked")
    masked: list[str] = Field(description="What was removed from it, such as 'name'")
    trace_id: str | None = Field(description="The answer's trace, when traces are exported")
    question_safety: Rating | None = Field(description="Azure's ratings of the question")
    answer_safety: Rating | None = Field(description="Azure's ratings of the model's answer")


@app.get("/health")
def health() -> dict[str, str | None]:
    return {"status": "ok", "version": get_settings().app_version or None}


def get_status() -> status.Status:
    return status.current(get_settings())


@app.get("/status", response_class=HTMLResponse)
def status_page(
    request: Request, current: Annotated[status.Status, Depends(get_status)]
) -> HTMLResponse:
    """How the service is checked, and how it's doing: aggregates only."""
    return templates.TemplateResponse(
        request,
        "status.html",
        {
            "nonce": request.state.csp_nonce,
            "snap": current.snapshot,
            "alarms": current.alarms,
            "trends": current.trends,
        },
    )


@app.get("/", response_class=HTMLResponse)
def page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {"max_question_chars": MAX_QUESTION_CHARS, "nonce": request.state.csp_nonce},
    )


@app.post("/ask")
def ask(
    request: Request,
    body: AskRequest,
    retriever: Annotated[Retriever, Depends(get_retriever)],
    chat: Annotated[Chat, Depends(get_chat)],
) -> AskResponse:
    settings = get_settings()
    telemetry.configure(settings)
    # FastAPI makes the request's span; this one holds what the app did with it.
    with (
        telemetry.synthetic(request.headers.get(telemetry.SYNTHETIC_HEADER)),
        telemetry.tracer().start_as_current_span("answer") as span,
    ):
        telemetry.set_app(span, requested_provider=body.provider)
        try:
            result = answer(body.question, retriever, chat_for(body.provider, chat))
        except (openai.APIError, AzureError, BotoCoreError, ClientError) as e:
            log.exception("Answering failed: %s", type(e).__name__)
            raise HTTPException(status_code=503, detail=UNAVAILABLE) from e
        telemetry.set_app(
            span,
            declined=result.declined,
            decline_reason=result.decline_reason,
            provider=result.provider,
            prompt_version=result.prompt_version,
            rewrite_version=result.rewrite_version,
            cited_sources=list(dict.fromkeys(c.passage.source_id for c in result.citations)),
            question_rated=result.question_safety is not None,
            question_flags=safety_flags(result.question_safety),
            answer_flags=safety_flags(result.answer_safety),
        )
        trace_id = telemetry.trace_id() if telemetry.exporting(settings) else None
    return AskResponse(
        question=result.question,
        answer=result.text,
        declined=result.declined,
        decline_reason=result.decline_reason,
        sources=[
            Source(
                number=c.number,
                source_id=c.passage.source_id,
                title=c.passage.title,
                section=c.passage.section,
                publisher=c.passage.publisher,
                url=c.passage.url,
                passage_id=c.passage.id,
                content=c.passage.content,
            )
            for c in result.citations
        ],
        retrieved=[
            Retrieved(passage_id=p.id, source_id=p.source_id, similarity=p.similarity)
            for p in result.passages
        ],
        top_similarity=result.top_similarity,
        search_query=result.search_query,
        prompt_version=result.prompt_version,
        rewrite_version=result.rewrite_version,
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        provider=result.provider,
        answered_by=(
            f"{model_label(result.model)} on {PROVIDER_NAMES[result.provider]}"
            if result.model and result.provider in PROVIDER_NAMES
            else ""
        ),
        failover_reason=result.failover_reason,
        sent_question=result.sent_question,
        masked=list(result.masked),
        trace_id=trace_id,
        question_safety=rating(result.question_safety),
        answer_safety=rating(result.answer_safety),
    )


# What uvicorn serves: the app, with each request's spans sent before its response
# completes (see telemetry.FlushSpans).
server = telemetry.FlushSpans(app)
