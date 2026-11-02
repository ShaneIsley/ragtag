"""Configuration for the app and the ingest job, read from environment variables.

Variable names match `.env.example` and Terraform's `local_env` output. Every
field has a default so tests and tooling run without a `.env`; code that calls
a service checks the settings it needs at that point.

In AWS, secrets aren't in the environment. For a secret setting such as
AZURE_OPENAI_API_KEY, the environment holds AZURE_OPENAI_API_KEY_SECRET_ARN, and
the value is read from Secrets Manager once, at start-up.
"""

import os
from collections.abc import Callable, Mapping
from functools import lru_cache
from typing import Any

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# The Azure OpenAI data-plane API version the app and ingest job are written against.
AZURE_OPENAI_API_VERSION = "2024-10-21"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Azure OpenAI
    azure_openai_endpoint: str = ""
    azure_openai_api_key: SecretStr = SecretStr("")
    azure_openai_chat_deployment: str = ""
    azure_openai_embedding_deployment: str = ""

    # Azure AI Search. The app uses the query key; only ingest needs the admin key.
    search_endpoint: str = ""
    search_index: str = "policy-desk-chunks"
    search_query_key: SecretStr = SecretStr("")
    search_admin_key: SecretStr = SecretStr("")

    # Amazon Bedrock failover
    bedrock_region: str = "ap-southeast-2"
    bedrock_model_id: str = "au.anthropic.claude-haiku-4-5-20251001-v1:0"
    # A failover drill: Azure OpenAI behaves as if unreachable, so every answer comes
    # from Bedrock. For running locally; never set in production.
    failover_drill: bool = False

    # The release this is (the image tag), shown by /health so an evaluation run can
    # record what it measured. Empty when running from a working tree.
    app_version: str = ""

    # The evaluation records bucket. The ingest job records there what each run
    # changed, for the drift checks (evals/drift.py), and the app reads the status
    # page's snapshot from it. Empty locally: nothing is sent or read.
    eval_records_bucket: str = ""
    # Locally, a status snapshot to show at /status (make status writes one).
    status_snapshot_file: str = ""

    # Flagged answers
    feedback_table: str = "policy-desk-feedback"
    feedback_retention_days: int = 90

    # Admin sign-in
    entra_tenant_id: str = ""
    entra_client_id: str = ""
    entra_client_secret: SecretStr = SecretStr("")
    entra_admin_role: str = "PolicyDesk.Admin"
    public_base_url: str = "http://localhost:8001"
    session_secret: SecretStr = SecretStr("")

    # OpenTelemetry
    # Where spans go: none, console, or otlp to the endpoint below (app/telemetry.py).
    otel_traces_exporter: str = "none"
    otel_exporter_otlp_endpoint: str = "http://localhost:4318"
    otel_exporter_otlp_protocol: str = "http/protobuf"
    otel_service_name: str = "policy-desk-app-local"


SECRET_ARN_SUFFIX = "_SECRET_ARN"

# Fetches secret values by ARN: {arn: value}.
SecretFetcher = Callable[[list[str]], dict[str, str]]


def secrets_from_arns(
    environ: Mapping[str, str], fetch: SecretFetcher | None = None
) -> dict[str, str]:
    """Setting values for every NAME_SECRET_ARN in the environment whose NAME isn't set
    directly, keyed by field name."""
    wanted = {
        key.removesuffix(SECRET_ARN_SUFFIX).lower(): arn
        for key, arn in environ.items()
        if key.endswith(SECRET_ARN_SUFFIX)
        and arn
        and not environ.get(key.removesuffix(SECRET_ARN_SUFFIX))
        and key.removesuffix(SECRET_ARN_SUFFIX).lower() in Settings.model_fields
    }
    if not wanted:
        return {}
    values = (fetch or fetch_from_secrets_manager)(sorted(set(wanted.values())))
    return {name: values[arn] for name, arn in wanted.items()}


def fetch_from_secrets_manager(arns: list[str]) -> dict[str, str]:
    import boto3  # only in AWS; local runs read .env

    client = boto3.client("secretsmanager")
    response = client.batch_get_secret_value(SecretIdList=arns)
    if response.get("Errors"):
        failed = ", ".join(e.get("SecretId", "?") for e in response["Errors"])
        raise RuntimeError(f"Couldn't read secrets: {failed}")
    return {s["ARN"]: s["SecretString"] for s in response["SecretValues"]}


@lru_cache
def get_settings() -> Settings:
    overrides: dict[str, Any] = secrets_from_arns(os.environ)
    return Settings(**overrides)


def require(settings: Settings, *names: str) -> None:
    """Fail early, naming the variables, when a service's settings are missing."""
    missing = [n.upper() for n in names if not _value(getattr(settings, n))]
    if missing:
        raise RuntimeError(f"Missing settings: {', '.join(missing)}. See .env.example.")


def _value(v: object) -> str:
    return v.get_secret_value() if isinstance(v, SecretStr) else str(v)
