"""OpenTelemetry traces: FastAPI's span for each request, and the app's own for each
step of answering.

Spans carry the GenAI attribute names (operation, provider, model, token counts) and
the app's own (prompt versions, passage IDs, what was masked, why a question was
declined). They never carry the question or the answer. FastAPI's request span
records the path and status but no client address, so no visitor IP address either
(tests/test_app_telemetry.py checks both).

OTEL_TRACES_EXPORTER picks where spans go: "none" (the default), "console", or "otlp"
to OTEL_EXPORTER_OTLP_ENDPOINT, such as the collector in compose.yaml. An X-Ray
endpoint (https://xray.<region>.amazonaws.com) gets each export signed with the
function's own AWS credentials, which is how Lambda sends spans to CloudWatch.
"""

import asyncio
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlsplit

import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from botocore.session import get_session
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as gen_ai
from opentelemetry.trace import Span
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.settings import Settings

TRACER_NAME = "policy_desk"
GEN_AI_PROVIDERS = {"azure": "azure.ai.openai", "bedrock": "aws.bedrock"}
XRAY_HOST = re.compile(r"xray\.([a-z0-9-]+)\.amazonaws\.com")
# How long a response may wait for its spans to be sent.
FLUSH_TIMEOUT_MS = 2000

_provider: TracerProvider | None = None
_served_a_request = False


def xray_region(endpoint: str) -> str | None:
    """The region of an X-Ray OTLP endpoint, or None for any other collector."""
    match = XRAY_HOST.fullmatch(urlsplit(endpoint).hostname or "")
    return match.group(1) if match else None


class SigV4(requests.auth.AuthBase):
    """Signs each export to X-Ray's OTLP endpoint, which accepts only signed requests."""

    def __init__(self, region: str, credentials: Credentials | None = None) -> None:
        credentials = credentials or get_session().get_credentials()
        if credentials is None:
            raise RuntimeError("Sending spans to X-Ray needs AWS credentials")
        self._credentials = credentials
        self._region = region

    def __call__(self, r: requests.PreparedRequest) -> requests.PreparedRequest:
        signed = AWSRequest(
            method=r.method,
            url=r.url,
            data=r.body,
            headers={"Content-Type": r.headers.get("Content-Type", "")},
        )
        auth = SigV4Auth(self._credentials.get_frozen_credentials(), "xray", self._region)
        auth.add_auth(signed)
        r.headers.update(dict(signed.headers.items()))
        return r


def configure(settings: Settings) -> TracerProvider:
    """Sets up tracing once per process; later calls return the same provider."""
    global _provider
    if _provider is not None:
        return _provider
    _provider = TracerProvider(
        resource=Resource.create({"service.name": settings.otel_service_name})
    )
    exporter = settings.otel_traces_exporter
    if exporter == "otlp":
        endpoint = f"{settings.otel_exporter_otlp_endpoint.rstrip('/')}/v1/traces"
        session = requests.Session()
        region = xray_region(endpoint)
        if region:
            session.auth = SigV4(region)
        _provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, session=session))
        )
    elif exporter == "console":
        _provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(_provider)
    return _provider


def add_processor(settings: Settings, processor: SpanProcessor) -> None:
    """Sends spans somewhere else as well, such as to a test's in-memory exporter."""
    configure(settings).add_span_processor(processor)


def flush() -> None:
    """Sends every span that has ended and is still queued."""
    if _provider is not None:
        _provider.force_flush(FLUSH_TIMEOUT_MS)


class FlushSpans:
    """Holds back the end of each response until the request's spans are sent.

    Lambda pauses the function as soon as a response is complete, so spans still
    queued would wait for the next request, or be lost when the function is retired.
    FastAPI ends its request span after the app has sent the response, which is why
    this wraps the whole app rather than a route. Costs one export per request: a few
    tens of milliseconds within the region.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        held: list[Message] = []

        async def hold_the_end(message: Message) -> None:
            if message["type"] == "http.response.body" and not message.get("more_body"):
                held.append(message)
            else:
                await send(message)

        try:
            await self.app(scope, receive, hold_the_end)
        finally:
            await asyncio.to_thread(flush)
            for message in held:
                await send(message)


def mark_cold_start() -> None:
    """Marks the first request this process serves, on its request span. On Lambda,
    that's the request that waited for the function to start."""
    global _served_a_request
    if not _served_a_request:
        _served_a_request = True
        trace.get_current_span().set_attribute("faas.coldstart", True)


def exporting(settings: Settings) -> bool:
    return settings.otel_traces_exporter in ("otlp", "console")


def tracer() -> trace.Tracer:
    return trace.get_tracer(TRACER_NAME)


# A request the evaluation makes sends this header naming its run (evals/provider.py).
# Its spans carry app.synthetic, so usage and drift figures can leave the golden
# questions out. Anyone can send it; it changes nothing but how a request is counted.
SYNTHETIC_HEADER = "x-policy-desk-run"
_synthetic: ContextVar[str | None] = ContextVar("synthetic", default=None)


@contextmanager
def synthetic(run: str | None) -> Iterator[None]:
    """Marks the spans made inside it as synthetic, labelled with the run's name."""
    label = re.sub(r"[^A-Za-z0-9:_-]", "", run or "")[:40] or None
    token = _synthetic.set(label)
    try:
        yield
    finally:
        _synthetic.reset(token)


@contextmanager
def step(name: str, **attributes: Any) -> Iterator[Span]:
    """A span for one step of answering, with the app's attributes (prefixed app.)."""
    with tracer().start_as_current_span(name) as span:
        set_app(span, **attributes)
        yield span


def set_app(span: Span, **attributes: Any) -> None:
    attributes.setdefault("synthetic", _synthetic.get())
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            value = [str(v) for v in value]
        span.set_attribute(f"app.{key}", value)


def set_model_call(
    span: Span,
    *,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    failover_reason: str | None = None,
) -> None:
    """The GenAI semantic-convention attributes for one chat call."""
    span.set_attribute(gen_ai.GEN_AI_OPERATION_NAME, "chat")
    if provider in GEN_AI_PROVIDERS:
        span.set_attribute(gen_ai.GEN_AI_PROVIDER_NAME, GEN_AI_PROVIDERS[provider])
    span.set_attribute(gen_ai.GEN_AI_RESPONSE_MODEL, model)
    span.set_attribute(gen_ai.GEN_AI_USAGE_INPUT_TOKENS, input_tokens)
    span.set_attribute(gen_ai.GEN_AI_USAGE_OUTPUT_TOKENS, output_tokens)
    if failover_reason:
        span.set_attribute("app.failover_reason", failover_reason)


def set_embedding_call(span: Span, *, model: str, input_tokens: int) -> None:
    """The GenAI semantic-convention attributes for one embedding call."""
    span.set_attribute(gen_ai.GEN_AI_OPERATION_NAME, "embeddings")
    span.set_attribute(gen_ai.GEN_AI_PROVIDER_NAME, GEN_AI_PROVIDERS["azure"])
    span.set_attribute(gen_ai.GEN_AI_RESPONSE_MODEL, model)
    span.set_attribute(gen_ai.GEN_AI_USAGE_INPUT_TOKENS, input_tokens)


def trace_id() -> str | None:
    """The current trace's ID, as Jaeger and X-Ray search for it."""
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else None
