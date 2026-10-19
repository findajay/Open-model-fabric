"""The explicit list of adapter types compiled into this build.

To add an adapter: implement the port in ``omf/adapters/...``, then register it here.
No domain or application service changes are needed. Configuration selects adapters by
these ids only; it cannot load arbitrary modules (ADR-0004).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from omf.adapters.compute import colab, local
from omf.adapters.inference import mock, ollama, openai_compatible
from omf.adapters.storage import google_drive, local_fs, s3
from omf.core.domain.errors import ConfigurationError, ValidationFailed
from omf.core.domain.models import Locality
from omf.core.ports.compute import ComputeProvider
from omf.core.ports.registry import AdapterCatalog, AdapterContext, InferenceAdapterDescriptor
from omf.core.ports.storage import ArtifactStorageProvider

INFERENCE_ADAPTERS = [
    InferenceAdapterDescriptor(
        adapter_type=mock.ADAPTER_TYPE,
        display_name="Mock (no GPU)",
        version=mock.ADAPTER_VERSION,
        description=(
            "Deterministic in-process adapter for development, tests and demos. "
            "Does not run a model."
        ),
        config_model=mock.MockConfig,
        capabilities=mock.CAPABILITIES,
        factory=mock.create,
        default_locality=Locality.LOCAL,
    ),
    InferenceAdapterDescriptor(
        adapter_type=openai_compatible.ADAPTER_TYPE,
        display_name="OpenAI-compatible",
        version=openai_compatible.ADAPTER_VERSION,
        description="Any server implementing /v1/chat/completions and /v1/models.",
        config_model=openai_compatible.OpenAICompatibleConfig,
        capabilities=openai_compatible.CAPABILITIES,
        factory=openai_compatible.create,
        # Fail safe: treat as external (no sensitive prompts) unless declared local.
        default_locality=Locality.EXTERNAL,
        endpoint_fields=("base_url",),
        secret_fields=("api_key_ref",),
        future_compatible_with=(
            "vllm",
            "text-generation-inference",
            "llama.cpp server",
            "lm-studio",
            "localai",
            "sglang",
            "hugging-face-inference-endpoints",
            "custom OpenAI-compatible servers",
        ),
    ),
    InferenceAdapterDescriptor(
        adapter_type=ollama.ADAPTER_TYPE,
        display_name="Ollama",
        version=ollama.ADAPTER_VERSION,
        description="Ollama native API; model capabilities are read from /api/show.",
        config_model=ollama.OllamaConfig,
        capabilities=ollama.CAPABILITIES,
        factory=ollama.create,
        default_locality=Locality.LOCAL,
        endpoint_fields=("base_url",),
    ),
]


def inference_catalog() -> AdapterCatalog:
    return AdapterCatalog(INFERENCE_ADAPTERS)


# ---------------------------------------------------------------- compute / storage

ComputeFactory = Callable[[str, dict[str, Any], AdapterContext], ComputeProvider]
StorageFactory = Callable[[str, dict[str, Any], AdapterContext], ArtifactStorageProvider]


def _parse(model: type[BaseModel], raw: dict[str, Any], what: str) -> Any:
    try:
        return model.model_validate(raw)
    except Exception as exc:
        raise ValidationFailed(
            f"invalid configuration for {what}", details={"error": str(exc)}
        ) from exc


def _local_compute(env_id: str, raw: dict[str, Any], _ctx: AdapterContext) -> ComputeProvider:
    return local.LocalComputeAdapter(env_id, _parse(local.LocalComputeConfig, raw, "local compute"))


def _colab_compute(env_id: str, raw: dict[str, Any], ctx: AdapterContext) -> ComputeProvider:
    config = _parse(colab.ColabNotebookConfig, raw, "colab compute")
    if config.endpoint:
        ctx.egress.check_url_syntax(config.endpoint.url)
    return colab.ColabNotebookComputeAdapter(env_id, config, ctx.egress)


COMPUTE_ADAPTERS: dict[str, ComputeFactory] = {
    local.ADAPTER_TYPE: _local_compute,
    colab.ADAPTER_TYPE: _colab_compute,
}


def _local_storage(sid: str, raw: dict[str, Any], _ctx: AdapterContext) -> ArtifactStorageProvider:
    return local_fs.LocalFilesystemStorageAdapter(
        sid, _parse(local_fs.LocalFilesystemConfig, raw, "local_fs")
    )


def _gdrive_storage(sid: str, raw: dict[str, Any], ctx: AdapterContext) -> ArtifactStorageProvider:
    config = _parse(google_drive.GoogleDriveConfig, raw, "google_drive")
    ctx.egress.check_url_syntax(config.api_base)
    credentials = ctx.resolve_secret(config.credentials_ref)
    return google_drive.GoogleDriveStorageAdapter(
        sid, config, google_drive.ServiceAccountTokenSource(credentials, config.scope)
    )


def _s3_storage(sid: str, raw: dict[str, Any], ctx: AdapterContext) -> ArtifactStorageProvider:
    config = _parse(s3.S3Config, raw, "s3")
    if config.endpoint_url:
        ctx.egress.check_url_syntax(config.endpoint_url)
    access = ctx.resolve_secret(config.access_key_id_ref) if config.access_key_id_ref else None
    secret = (
        ctx.resolve_secret(config.secret_access_key_ref) if config.secret_access_key_ref else None
    )
    return s3.S3StorageAdapter.from_config(sid, config, access, secret)


STORAGE_ADAPTERS: dict[str, StorageFactory] = {
    local_fs.ADAPTER_TYPE: _local_storage,
    google_drive.ADAPTER_TYPE: _gdrive_storage,
    s3.ADAPTER_TYPE: _s3_storage,
}


def build_compute(
    env_id: str, adapter: str, raw: dict[str, Any], ctx: AdapterContext
) -> ComputeProvider:
    if adapter not in COMPUTE_ADAPTERS:
        raise ConfigurationError(
            f"unknown compute adapter {adapter!r}", details={"available": sorted(COMPUTE_ADAPTERS)}
        )
    return COMPUTE_ADAPTERS[adapter](env_id, raw, ctx)


def build_storage(
    sid: str, adapter: str, raw: dict[str, Any], ctx: AdapterContext
) -> ArtifactStorageProvider:
    if adapter not in STORAGE_ADAPTERS:
        raise ConfigurationError(
            f"unknown storage adapter {adapter!r}", details={"available": sorted(STORAGE_ADAPTERS)}
        )
    return STORAGE_ADAPTERS[adapter](sid, raw, ctx)


def describe_component_adapters() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    groups: list[tuple[str, dict[str, Any], dict[str, type[BaseModel]]]] = [
        (
            "compute",
            COMPUTE_ADAPTERS,
            {
                local.ADAPTER_TYPE: local.LocalComputeConfig,
                colab.ADAPTER_TYPE: colab.ColabNotebookConfig,
            },
        ),
        (
            "storage",
            STORAGE_ADAPTERS,
            {
                local_fs.ADAPTER_TYPE: local_fs.LocalFilesystemConfig,
                google_drive.ADAPTER_TYPE: google_drive.GoogleDriveConfig,
                s3.ADAPTER_TYPE: s3.S3Config,
            },
        ),
    ]
    for kind, mapping, models in groups:
        for adapter_type in sorted(mapping):
            out.append(
                {
                    "adapter_type": adapter_type,
                    "kind": kind,
                    "config_schema": models[adapter_type].model_json_schema(),
                }
            )
    return out
