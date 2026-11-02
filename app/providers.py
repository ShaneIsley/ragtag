"""The second model and the failover.

Azure OpenAI answers first. If it fails (no connection, a timeout, rate limiting
after the SDK's retries, or a server error), Claude Haiku 4.5 on Amazon Bedrock
answers instead, through the AU inference profile (Sydney or Melbourne). A prompt
that Azure's content filter blocks never fails over: that's a decline, and asking
another model would route around the filter. Bedrock's answers have no Azure
content safety ratings.
"""

import dataclasses
import logging
from typing import Any

import boto3
import httpx
import openai
from botocore.config import Config

from app.answer import Completion, Effort
from app.settings import Settings

log = logging.getLogger("app")

PROVIDER_NAMES = {"azure": "Azure OpenAI", "bedrock": "Amazon Bedrock"}

# Errors that mean Azure OpenAI couldn't answer, as opposed to refusing to.
AZURE_FAILURES = (
    openai.APIConnectionError,  # includes timeouts
    openai.RateLimitError,
    openai.InternalServerError,
)


def model_label(model: str) -> str:
    """A model's name as the page shows it."""
    if "claude-haiku-4-5" in model:
        return "Claude Haiku 4.5"
    if model.startswith("gpt-5-mini"):
        return "GPT-5 mini"
    return model


class BedrockChat:
    """Claude on Amazon Bedrock, through the Converse API."""

    def __init__(self, settings: Settings, client: Any = None) -> None:
        self._client = client or boto3.client(
            "bedrock-runtime",
            region_name=settings.bedrock_region,
            config=Config(read_timeout=60, retries={"max_attempts": 3, "mode": "standard"}),
        )
        self._model = settings.bedrock_model_id

    def complete(self, system: str, user: str, *, effort: Effort = "low") -> Completion:
        response = self._client.converse(
            modelId=self._model,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={"maxTokens": 2000},
        )
        blocks = response["output"]["message"]["content"]
        usage = response.get("usage", {})
        return Completion(
            text="".join(b.get("text", "") for b in blocks),
            model=self._model,
            input_tokens=int(usage.get("inputTokens", 0)),
            output_tokens=int(usage.get("outputTokens", 0)),
            provider="bedrock",
        )


class FailoverChat:
    """The primary model, or the secondary when the primary fails."""

    def __init__(self, primary: Any, secondary: Any) -> None:
        self._primary = primary
        self._secondary = secondary

    def complete(self, system: str, user: str, *, effort: Effort = "low") -> Completion:
        try:
            completion: Completion = self._primary.complete(system, user, effort=effort)
            return completion
        except AZURE_FAILURES as e:
            reason = f"Azure OpenAI didn't respond ({type(e).__name__})"
            log.warning("Failing over to Bedrock: %s", reason)
            answered: Completion = self._secondary.complete(system, user, effort=effort)
            return dataclasses.replace(answered, failover_reason=reason)


class DownChat:
    """Stands in for Azure OpenAI in a failover drill (FAILOVER_DRILL=1): every call
    fails as if the service were unreachable."""

    def complete(self, system: str, user: str, *, effort: Effort = "low") -> Completion:
        request = httpx.Request("POST", "https://drill.invalid/openai")
        raise openai.APIConnectionError(message="Failover drill", request=request)  # type: ignore[arg-type]
