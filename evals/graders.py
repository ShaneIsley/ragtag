"""Groundedness and relevance, scored 1 to 5 by the Azure AI Evaluation SDK's graders.

Both run on the app's chat deployment. Groundedness is judged against the passages
the answer cites, since answer.v2 requires a citation for every statement. Tried on
31 Oct 2026 before use: a grounded answer scored 4 and one with an invented claim 2;
an on-topic answer scored 5 for relevance and an off-topic one 1.
"""

from dataclasses import dataclass
from typing import Any

from azure.ai.evaluation import (
    AzureOpenAIModelConfiguration,
    GroundednessEvaluator,
    RelevanceEvaluator,
)

from app.settings import AZURE_OPENAI_API_VERSION, Settings


@dataclass(frozen=True)
class Grade:
    score: float  # 1 to 5
    reason: str


def model_config(settings: Settings) -> AzureOpenAIModelConfiguration:
    return AzureOpenAIModelConfiguration(
        azure_endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key.get_secret_value(),
        azure_deployment=settings.azure_openai_chat_deployment,
        api_version=AZURE_OPENAI_API_VERSION,
    )


def _grade(result: dict[str, Any], name: str) -> Grade:
    return Grade(float(result[name]), str(result.get(f"{name}_reason", "")))


def groundedness(settings: Settings, question: str, answer: str, context: str) -> Grade:
    grader = GroundednessEvaluator(model_config(settings), is_reasoning_model=True)
    return _grade(grader(query=question, response=answer, context=context), "groundedness")


def relevance(settings: Settings, question: str, answer: str) -> Grade:
    grader = RelevanceEvaluator(model_config(settings), is_reasoning_model=True)
    return _grade(grader(query=question, response=answer), "relevance")


def cited_context(response: dict[str, Any]) -> str:
    """The text of every passage the answer cites, numbered as the answer cites them."""
    return "\n\n".join(
        f"[{s.get('number')}] {s.get('content', '')}" for s in response.get("sources", [])
    )
