"""Sends one test span to X-Ray's OTLP endpoint, signed the way the app signs its
exports, and prints X-Ray's reply: the HTTP status and any spans it rejected. The
app's exporter logs a failed request but not spans rejected inside a successful one,
so this shows what it can't. Needs AWS credentials allowed xray:PutSpans.

    uv run python scripts/xray_span_check.py [region]

Then search aws/spans for the printed trace ID; spans can take a few minutes.
"""

import sys
from pathlib import Path

import requests
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.telemetry import SigV4


def main(region: str) -> int:
    spans = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "policy-desk-span-check"}))
    provider.add_span_processor(SimpleSpanProcessor(spans))
    with provider.get_tracer("span-check").start_as_current_span("span check") as span:
        trace_id = format(span.get_span_context().trace_id, "032x")
    body = encode_spans(spans.get_finished_spans()).SerializeToString()

    session = requests.Session()
    session.auth = SigV4(region)
    reply = session.post(
        f"https://xray.{region}.amazonaws.com/v1/traces",
        data=body,
        headers={"Content-Type": "application/x-protobuf"},
        timeout=15,
    )
    print(f"HTTP {reply.status_code}")
    if not reply.ok:
        print(reply.text[:500])
        return 1
    answer = ExportTraceServiceResponse()
    try:
        answer.ParseFromString(reply.content)
    except Exception:  # show whatever came back
        print(reply.content[:500])
        return 1
    rejected = answer.partial_success.rejected_spans
    print(f"Rejected spans: {rejected} {answer.partial_success.error_message}".rstrip())
    print(f"Trace ID: {trace_id}")
    return 1 if rejected else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "ap-southeast-2"))
