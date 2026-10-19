"""Acceptance criterion 12: configuration changes are validated."""

from __future__ import annotations

from pathlib import Path

import pytest

from omf.bootstrap.config import load_config, parse_config
from omf.core.domain.errors import ConfigurationError
from tests.conftest import base_config

CONFIGS = Path(__file__).resolve().parents[2] / "configs"


@pytest.mark.parametrize(
    "profile", ["development", "local-ollama", "colab-experiment", "production.example"]
)
def test_shipped_profiles_are_valid(profile: str) -> None:
    env = {
        "OMF_DATA_DIR": "/tmp/omf-test",
        "VLLM_BASE_URL": "https://vllm.internal.example.com/v1",
        "S3_BUCKET": "omf-artifacts",
        "GDRIVE_FOLDER_ID": "0ABCdefGhIjKlMnOpQ",
        "COLAB_ENDPOINT_URL": "https://example.invalid",
    }
    config = load_config(CONFIGS / f"{profile}.yaml", env)
    assert config.profile


def test_inline_secret_is_rejected(tmp_path: Path) -> None:
    raw = base_config(tmp_path)
    raw["providers"].append(
        {
            "id": "openai",
            "adapter_type": "openai_compatible",
            "config": {"base_url": "https://api.example.com/v1", "api_key": "sk-live-123"},
        }
    )
    with pytest.raises(ConfigurationError) as info:
        parse_config(raw, {})
    assert "providers[1].config.api_key" in info.value.details["fields"]


def test_secret_references_are_allowed(tmp_path: Path) -> None:
    raw = base_config(tmp_path)
    raw["providers"].append(
        {
            "id": "openai",
            "adapter_type": "openai_compatible",
            "config": {
                "base_url": "https://api.example.com/v1",
                "api_key_ref": "env:OPENAI_API_KEY",
            },
        }
    )
    config = parse_config(raw, {})
    assert "env:OPENAI_API_KEY" in config.secret_references()


def test_env_substitution_with_default(tmp_path: Path) -> None:
    raw = base_config(tmp_path, inference={"default_provider": "${DEFAULT_PROVIDER:-mock}"})
    assert parse_config(raw, {}).inference.default_provider == "mock"
    with pytest.raises(ConfigurationError):
        parse_config(base_config(tmp_path, inference={"default_provider": "${MISSING_VAR}"}), {})


def test_unknown_default_provider_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        parse_config(base_config(tmp_path, inference={"default_provider": "nope"}), {})


def test_fallback_requires_explicit_opt_in(tmp_path: Path) -> None:
    raw = base_config(
        tmp_path,
        inference={"default_provider": "mock", "routing": {"fallback_provider_ids": ["mock"]}},
    )
    with pytest.raises(ConfigurationError):
        parse_config(raw, {})


def test_production_requires_authentication(tmp_path: Path) -> None:
    raw = base_config(tmp_path, environment="production")
    with pytest.raises(ConfigurationError, match="invalid configuration"):
        parse_config(raw, {})


def test_auth_none_only_on_localhost(tmp_path: Path) -> None:
    raw = base_config(tmp_path, server={"host": "0.0.0.0"})
    with pytest.raises(ConfigurationError):
        parse_config(raw, {})


def test_database_url_with_password_rejected(tmp_path: Path) -> None:
    raw = base_config(tmp_path, metadata={"url": "postgresql+psycopg://omf:hunter2@db/omf"})
    with pytest.raises(ConfigurationError):
        parse_config(raw, {})


def test_unknown_fields_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        parse_config(base_config(tmp_path, servre={"port": 1}), {})
