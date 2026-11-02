"""promptfoo provider: asks the running app one question over HTTP.

The target is EVAL_TARGET_URL: the local app by default, or the CloudFront address
for production. EVAL_PROVIDER picks the model: auto (the default), azure or bedrock.
The body's SHA-256 goes in x-amz-content-sha256 because CloudFront signs requests to
the app and needs it for a POST; the local app ignores it. x-policy-desk-run names the
run, so the app marks its spans synthetic and the drift checks leave them out.
"""

import hashlib
import json
import os
import time
from typing import Any

import httpx

DEFAULT_TARGET = "http://127.0.0.1:8001"
TIMEOUT = httpx.Timeout(90.0, connect=10.0)


def ask(
    question: str, client: httpx.Client, target: str, provider: str = "auto", run: str = "eval"
) -> dict[str, Any]:
    body = json.dumps({"question": question, "provider": provider})
    headers = {
        "Content-Type": "application/json",
        "x-amz-content-sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "x-policy-desk-run": run,
    }
    start = time.perf_counter()
    try:
        response = client.post(f"{target.rstrip('/')}/ask", content=body, headers=headers)
    except httpx.HTTPError as e:
        return {"error": f"Couldn't reach the app at {target}: {type(e).__name__}"}
    latency_ms = (time.perf_counter() - start) * 1000
    if response.status_code != 200:
        return {"error": f"The app returned HTTP {response.status_code}: {response.text[:200]}"}
    data = response.json()
    return {
        "output": data["answer"],
        "latencyMs": round(latency_ms),
        "tokenUsage": {
            "prompt": data["input_tokens"],
            "completion": data["output_tokens"],
            "total": data["input_tokens"] + data["output_tokens"],
        },
        "metadata": {"response": data, "latency_ms": latency_ms},
    }


def call_api(prompt: str, options: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    target = os.environ.get("EVAL_TARGET_URL", DEFAULT_TARGET)
    # auto (Azure OpenAI with failover), azure, or bedrock: for comparing the models.
    provider = os.environ.get("EVAL_PROVIDER", "auto")
    run = f"eval:{os.environ.get('EVAL_SUITE', 'full')}"
    with httpx.Client(timeout=TIMEOUT) as client:
        return ask(prompt, client, target, provider, run)
