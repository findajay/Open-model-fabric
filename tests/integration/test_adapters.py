"""Adapter-level tests: each adapter independently, against emulators or in isolation."""

from __future__ import annotations

from pathlib import Path

import pytest

from omf.adapters.compute.colab import ColabNotebookComputeAdapter, ColabNotebookConfig
from omf.adapters.compute.local import LocalComputeAdapter, LocalComputeConfig
from omf.adapters.inference.ollama import OllamaConfig
from omf.adapters.inference.ollama import create as create_ollama
from omf.adapters.inference.openai_compatible import OpenAICompatibleConfig
from omf.adapters.inference.openai_compatible import create as create_openai
from omf.adapters.secrets.env import EnvSecretProvider, FileSecretProvider
from omf.adapters.telemetry.log_telemetry import LoggingTelemetry
from omf.core.domain.capabilities import Capability, Evidence
from omf.core.domain.errors import (
    ModelNotFound,
    OperationNotSupported,
    ProviderAuthenticationFailed,
    ProviderError,
    SecretNotFound,
)
from omf.core.domain.models import (
    ChatMessage,
    ComputeState,
    HealthStatus,
    InferenceRequest,
    ProviderDefinition,
    ResponseFormat,
    Role,
)
from omf.core.ports.registry import AdapterContext
from omf.core.ports.secrets import SecretRef, SecretValue
from omf.core.security.egress import EgressPolicy
from tests.conftest import ServerThread, emulator_state

CTX = AdapterContext(
    resolve_secret=lambda ref: SecretValue("sk-test-0123456789abcdef-SECRET"),
    egress=EgressPolicy(allow_loopback=True),
    telemetry=LoggingTelemetry(),
)


def request(model: str, text: str = "hello", **params: object) -> InferenceRequest:
    from omf.core.domain.models import GenerationParams

    return InferenceRequest(
        request_id="r1",
        model=model,
        messages=[ChatMessage(role=Role.USER, content=text)],
        params=GenerationParams(**params),
    )  # type: ignore[arg-type]


async def test_openai_adapter_mid_stream_error_is_normalised(openai_server: ServerThread) -> None:
    emulator_state(openai_server).fail_mid_stream_after = 2
    adapter = create_openai(
        ProviderDefinition(id="r", adapter_type="openai_compatible"),
        OpenAICompatibleConfig(base_url=f"{openai_server.url}/v1", api_key_ref="env:K"),
        CTX,
    )
    chunks = []
    with pytest.raises(ProviderError, match="mid-stream"):
        async for chunk in adapter.stream_generate(request("remote-small")):
            chunks.append(chunk.delta)
    assert len([c for c in chunks if c]) == 2
    await adapter.aclose()


async def test_openai_adapter_json_mode_and_unknown_model(openai_server: ServerThread) -> None:
    adapter = create_openai(
        ProviderDefinition(id="r", adapter_type="openai_compatible"),
        OpenAICompatibleConfig(base_url=f"{openai_server.url}/v1", api_key_ref="env:K"),
        CTX,
    )
    result = await adapter.generate(request("remote-small", response_format=ResponseFormat.JSON))
    import json

    assert json.loads(result.content)["engine"] == "openai-emulator"
    assert emulator_state(openai_server).requests[-1]["response_format"] == {"type": "json_object"}
    with pytest.raises(ModelNotFound):
        await adapter.generate(request("missing"))
    await adapter.aclose()


async def test_openai_adapter_bad_key(openai_server: ServerThread) -> None:
    ctx = AdapterContext(
        resolve_secret=lambda ref: SecretValue("wrong"), egress=CTX.egress, telemetry=CTX.telemetry
    )
    adapter = create_openai(
        ProviderDefinition(id="r", adapter_type="openai_compatible"),
        OpenAICompatibleConfig(base_url=f"{openai_server.url}/v1", api_key_ref="env:K"),
        ctx,
    )
    with pytest.raises(ProviderAuthenticationFailed):
        await adapter.list_models()
    health = await adapter.health_check()
    assert (
        health.status == HealthStatus.DOWN and health.error_code == "provider_authentication_failed"
    )
    await adapter.aclose()


async def test_openai_adapter_missing_secret_does_not_crash_startup() -> None:
    def missing(ref: str) -> SecretValue:
        raise SecretNotFound("nope")

    ctx = AdapterContext(resolve_secret=missing, egress=CTX.egress, telemetry=CTX.telemetry)
    adapter = create_openai(
        ProviderDefinition(id="r", adapter_type="openai_compatible"),
        OpenAICompatibleConfig(base_url="http://127.0.0.1:9/v1", api_key_ref="env:K"),
        ctx,
    )
    with pytest.raises(ProviderAuthenticationFailed, match="not available"):
        await adapter.list_models()
    assert (await adapter.health_check()).status == HealthStatus.DOWN
    await adapter.aclose()


async def test_openai_adapter_declared_models_without_models_endpoint(
    openai_server: ServerThread,
) -> None:
    from omf.core.domain.models import DeclaredDeployment

    definition = ProviderDefinition(
        id="r", adapter_type="openai_compatible", models=[DeclaredDeployment(name="remote-small")]
    )
    adapter = create_openai(
        definition,
        OpenAICompatibleConfig(
            base_url=f"{openai_server.url}/v1", api_key_ref="env:K", list_models=False
        ),
        CTX,
    )
    assert [m.name for m in await adapter.list_models()] == ["remote-small"]
    await adapter.aclose()


async def test_ollama_adapter_reports_capabilities_and_quantization(
    ollama_server: ServerThread,
) -> None:
    adapter = create_ollama(
        ProviderDefinition(id="o", adapter_type="ollama"),
        OllamaConfig(base_url=ollama_server.url),
        CTX,
    )
    models = {m.name: m for m in await adapter.list_models()}
    vision = models["qwen2.5vl:3b"]
    assert vision.capabilities.get(Capability.VISION_INPUT).evidence == Evidence.PROVIDER_REPORTED  # type: ignore[union-attr]
    assert vision.context_window == 128000 and vision.quantization == "Q4_K_M"
    assert models["qwen2.5:0.5b"].capabilities.supports(Capability.TOOL_CALLING)
    assert models["qwen2.5:0.5b"].capabilities_complete
    result = await adapter.generate(request("qwen2.5:0.5b", max_tokens=3))
    assert result.finish_reason == "length" and result.usage.completion_tokens == 3  # type: ignore[union-attr]
    assert emulator_state(ollama_server).requests[-1]["options"]["num_predict"] == 3
    with pytest.raises(ModelNotFound):
        await adapter.generate(request("nope:1b"))
    with pytest.raises(OperationNotSupported):
        await adapter.cancel("r1")
    await adapter.aclose()


async def test_ollama_without_capability_metadata_declares_only_chat(
    ollama_server: ServerThread,
) -> None:
    emulator_state(ollama_server).report_capabilities = False  # pre-0.6 Ollama
    adapter = create_ollama(
        ProviderDefinition(id="o", adapter_type="ollama"),
        OllamaConfig(base_url=ollama_server.url),
        CTX,
    )
    vision = {m.name: m for m in await adapter.list_models()}["qwen2.5vl:3b"]
    assert not vision.capabilities.supports(Capability.VISION_INPUT)  # not assumed
    assert vision.capabilities.get(Capability.CHAT).evidence == Evidence.DECLARED  # type: ignore[union-attr]
    assert not vision.capabilities_complete
    await adapter.aclose()


async def test_ollama_health_and_outage(ollama_server: ServerThread) -> None:
    adapter = create_ollama(
        ProviderDefinition(id="o", adapter_type="ollama"),
        OllamaConfig(base_url=ollama_server.url),
        CTX,
    )
    assert (await adapter.health_check()).status == HealthStatus.UP
    emulator_state(ollama_server).fail_with_status = 503
    health = await adapter.health_check()
    assert health.status == HealthStatus.DOWN and health.error_code == "provider_unavailable"
    await adapter.aclose()


async def test_egress_policy_enforced_at_connect_time() -> None:
    strict = AdapterContext(
        resolve_secret=CTX.resolve_secret, egress=EgressPolicy(), telemetry=CTX.telemetry
    )
    adapter = create_ollama(
        ProviderDefinition(id="o", adapter_type="ollama"),
        OllamaConfig(base_url="http://localhost:11434"),
        strict,
    )
    from omf.core.domain.errors import EgressDenied

    with pytest.raises(EgressDenied):
        await adapter.list_models()
    await adapter.aclose()


# ---------------------------------------------------------------- compute


async def test_local_compute_describes_machine() -> None:
    env = await LocalComputeAdapter("local", LocalComputeConfig()).describe_environment()
    assert env.state == ComputeState.ONLINE and env.resources is not None
    assert env.resources.cpu_count and env.resources.memory_total_bytes
    with pytest.raises(OperationNotSupported):
        await LocalComputeAdapter("local", LocalComputeConfig()).start()


async def test_colab_states(openai_server: ServerThread) -> None:
    egress = EgressPolicy(allow_loopback=True)
    online = ColabNotebookComputeAdapter(
        "c",
        ColabNotebookConfig.model_validate(
            {"endpoint": {"url": f"{openai_server.url}/v1/models", "authorized": True}}
        ),
        egress,
    )
    assert await online.get_status() == ComputeState.ONLINE
    nothing = ColabNotebookComputeAdapter("c", ColabNotebookConfig(), egress)
    env = await nothing.describe_environment()
    assert env.state == ComputeState.OFFLINE and env.ephemeral and env.managed == "manual"
    for op in (nothing.provision, nothing.start, nothing.stop):
        with pytest.raises(OperationNotSupported):
            await op()


# ---------------------------------------------------------------- secrets & telemetry


def test_file_secrets_are_confined_to_allowed_dirs(tmp_path: Path) -> None:
    allowed = tmp_path / "secrets"
    allowed.mkdir()
    (allowed / "token").write_text("s3cr3t\n")
    (tmp_path / "outside").write_text("nope")
    provider = FileSecretProvider([str(allowed)], [f"file:{allowed}/token"])
    assert provider.resolve_secret(SecretRef.parse(f"file:{allowed}/token")).reveal() == "s3cr3t"
    for ref in (f"file:{tmp_path}/outside", f"file:{allowed}/../outside", "file:/etc/passwd"):
        with pytest.raises(SecretNotFound):
            provider.resolve_secret(SecretRef.parse(ref))
    assert (
        repr(provider.resolve_secret(SecretRef.parse(f"file:{allowed}/token")))
        == "SecretValue('**********')"
    )


def test_env_secrets_list_references_without_values() -> None:
    provider = EnvSecretProvider(["env:A", "env:B", "file:/x"], {"A": "value-a"})
    infos = provider.list_secret_references()
    assert [(i.reference, i.available) for i in infos] == [("env:A", True), ("env:B", False)]
    assert "value-a" not in repr(infos)


def test_opentelemetry_exporter_records_metrics_and_spans() -> None:
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from omf.adapters.telemetry.otel import OpenTelemetryTelemetry

    reader, spans = InMemoryMetricReader(), InMemorySpanExporter()
    otel = OpenTelemetryTelemetry(metric_reader=reader, span_exporter=spans)
    otel.record_metric("omf.requests", 1, kind="counter", attributes={"provider_id": "mock"})
    otel.record_metric("omf.provider.latency", 12.5, unit="ms", attributes={"provider_id": "mock"})
    otel.record_trace(
        "omf.chat", duration_ms=12.5, attributes={"provider_id": "mock", "model": None}
    )
    names = {
        m.name
        for rm in reader.get_metrics_data().resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    }
    assert {"omf.requests", "omf.provider.latency"} <= names
    finished = spans.get_finished_spans()
    assert finished[0].name == "omf.chat" and finished[0].attributes["provider_id"] == "mock"
    otel.shutdown()
