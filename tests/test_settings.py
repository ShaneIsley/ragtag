"""The settings module, .env.example and Terraform's local_env must name the same variables."""

import re
from pathlib import Path

import pytest

from app.settings import Settings, secrets_from_arns

ROOT = Path(__file__).resolve().parents[1]


def env_example_names() -> set[str]:
    lines = (ROOT / ".env.example").read_text().splitlines()
    return {line.split("=", 1)[0] for line in lines if re.match(r"^[A-Z_]+=", line)}


def terraform_local_env_names() -> set[str]:
    outputs = (ROOT / "infra" / "outputs.tf").read_text()
    match = re.search(r'output "local_env".*?<<-EOT\n(.*?)\n\s*EOT', outputs, re.DOTALL)
    assert match, "local_env output not found in infra/outputs.tf"
    return {line.strip().split("=", 1)[0] for line in match.group(1).splitlines() if line.strip()}


def settings_names() -> set[str]:
    return {name.upper() for name in Settings.model_fields}


def test_env_example_matches_settings() -> None:
    assert env_example_names() == settings_names()


def test_terraform_local_env_matches_settings() -> None:
    assert terraform_local_env_names() == settings_names()


def test_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_INDEX", "test-index")
    monkeypatch.setenv("FEEDBACK_RETENTION_DAYS", "7")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "not-a-real-key")
    settings = Settings(_env_file=None)
    assert settings.search_index == "test-index"
    assert settings.feedback_retention_days == 7
    assert settings.azure_openai_api_key.get_secret_value() == "not-a-real-key"
    assert "not-a-real-key" not in repr(settings)


def test_secrets_are_read_by_arn_when_not_set_directly() -> None:
    env = {
        "AZURE_OPENAI_API_KEY_SECRET_ARN": "arn:key",
        "SEARCH_QUERY_KEY_SECRET_ARN": "arn:query",
        "SEARCH_QUERY_KEY": "set-directly",
        "NOT_A_SETTING_SECRET_ARN": "arn:other",
    }
    asked: list[list[str]] = []

    def fetch(arns: list[str]) -> dict[str, str]:
        asked.append(arns)
        return {arn: f"value-of-{arn}" for arn in arns}

    assert secrets_from_arns(env, fetch) == {"azure_openai_api_key": "value-of-arn:key"}
    assert asked == [["arn:key"]]  # one call, only for what's needed


def test_no_secret_arns_means_no_secrets_manager_call() -> None:
    def fetch(arns: list[str]) -> dict[str, str]:
        raise AssertionError("should not be called")

    assert secrets_from_arns({"AZURE_OPENAI_ENDPOINT": "https://x"}, fetch) == {}
