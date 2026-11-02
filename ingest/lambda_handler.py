"""Run the ingest job as a Lambda function, on EventBridge Scheduler's weekly event."""

from typing import Any

from ingest.__main__ import main


def handler(event: Any, context: Any) -> dict[str, str]:
    """A full run. Failed sources fail the invocation, so the Lambda error alarm fires."""
    if main([]) != 0:
        raise RuntimeError("Ingest finished with failed sources; see the log above.")
    return {"status": "ok"}
