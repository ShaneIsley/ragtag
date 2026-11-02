"""Azure OpenAI embeddings, shared by the ingest job and the app."""

from collections.abc import Sequence
from typing import Protocol

from openai import AzureOpenAI

from app import telemetry
from app.settings import AZURE_OPENAI_API_VERSION, Settings, require

EMBED_BATCH = 64


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class AzureEmbedder:
    def __init__(self, settings: Settings) -> None:
        require(
            settings,
            "azure_openai_endpoint",
            "azure_openai_api_key",
            "azure_openai_embedding_deployment",
        )
        self._client = AzureOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key.get_secret_value(),
            api_version=AZURE_OPENAI_API_VERSION,
            max_retries=5,
        )
        self._deployment = settings.azure_openai_embedding_deployment

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), EMBED_BATCH):
            batch = list(texts[start : start + EMBED_BATCH])
            with telemetry.tracer().start_as_current_span(f"embeddings {self._deployment}") as span:
                response = self._client.embeddings.create(model=self._deployment, input=batch)
                telemetry.set_embedding_call(
                    span, model=response.model, input_tokens=response.usage.prompt_tokens
                )
            vectors.extend(item.embedding for item in sorted(response.data, key=lambda d: d.index))
        return vectors
