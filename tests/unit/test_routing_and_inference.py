"""Routing policies, fallback safety, sensitive-prompt policy, cancellation (service level)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from omf.bootstrap.catalog import INFERENCE_ADAPTERS
from omf.bootstrap.config import parse_config
from omf.bootstrap.container import Container, build_container
from omf.core.domain.capabilities import Capability, CapabilitySet
from omf.core.domain.errors import (
    CapabilityNotSupported,
    NoRouteAvailable,
    PolicyViolation,
    ProviderError,
    ProviderUnavailable,
)
from omf.core.domain.events import StreamEventType
from omf.core.domain.models import (
    AuthRequirement,
    ChatMessage,
    DiscoveredModel,
    GenerationChunk,
    GenerationResult,
    HealthStatus,
    ImagePart,
    InferenceRequest,
    Modality,
    ProviderCapabilities,
    ProviderDefinition,
    ProviderHealth,
    ProviderOperation,
    Role,
    Sensitivity,
    TextPart,
)
from omf.core.ports.inference import InferenceProvider
from omf.core.ports.registry import AdapterCatalog, AdapterContext, InferenceAdapterDescriptor
from omf.core.services.inference import ChatCommand
from tests.conftest import base_config


class ScriptedConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fail: str | None = None  # "unavailable" | "error" | "midstream"
    models: list[str] = ["shared-model"]


CALLS: dict[str, int] = {}


class ScriptedProvider(InferenceProvider):
    def __init__(self, provider_id: str, config: ScriptedConfig) -> None:
        super().__init__(provider_id)
        self.config = config

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            adapter_type="scripted",
            adapter_version="0",
            operations=list(ProviderOperation),
            modalities=[Modality.TEXT],
            streaming=True,
            health_check=True,
            authentication=AuthRequirement.NONE,
            model_capabilities_source="declared",
        )

    async def list_models(self) -> list[DiscoveredModel]:
        return [
            DiscoveredModel(
                name=m,
                capabilities=CapabilitySet.declared(
                    [Capability.CHAT, Capability.STREAMING], "test"
                ),
            )
            for m in self.config.models
        ]

    def _maybe_fail(self) -> None:
        CALLS[self.provider_id] = CALLS.get(self.provider_id, 0) + 1
        if self.config.fail == "unavailable":
            raise ProviderUnavailable(f"{self.provider_id} down")
        if self.config.fail == "error":
            raise ProviderError(f"{self.provider_id} bad request")

    async def generate(self, request: InferenceRequest) -> GenerationResult:
        self._maybe_fail()
        return GenerationResult(content=f"answer from {self.provider_id}", finish_reason="stop")

    async def stream_generate(
        self, request: InferenceRequest
    ) -> AsyncGenerator[GenerationChunk, None]:
        self._maybe_fail()
        for i in range(5):
            if self.config.fail == "midstream" and i == 2:
                raise ProviderUnavailable("connection dropped mid-stream")
            yield GenerationChunk(delta=f"{self.provider_id}-{i} ")
            await asyncio.sleep(0.01)
        yield GenerationChunk(finish_reason="stop")

    async def health_check(self) -> ProviderHealth:
        status = HealthStatus.DOWN if self.config.fail == "unavailable" else HealthStatus.UP
        return ProviderHealth(provider_id=self.provider_id, status=status)


def _create(d: ProviderDefinition, c: ScriptedConfig, _ctx: AdapterContext) -> ScriptedProvider:
    return ScriptedProvider(d.id, c)


SCRIPTED = InferenceAdapterDescriptor(
    adapter_type="scripted",
    display_name="scripted",
    version="0",
    description="test",
    config_model=ScriptedConfig,
    capabilities=ScriptedProvider("x", ScriptedConfig()).get_capabilities(),
    factory=_create,
)


async def container(
    tmp_path: Path, providers: list[dict[str, Any]], routing: dict[str, Any], default: str = "a"
) -> Container:
    CALLS.clear()
    raw = base_config(
        tmp_path, providers=providers, inference={"default_provider": default, "routing": routing}
    )
    return await build_container(
        parse_config(raw, {}), catalog=AdapterCatalog([*INFERENCE_ADAPTERS, SCRIPTED])
    )


def p(pid: str, fail: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"id": pid, "adapter_type": "scripted", "config": {"fail": fail}, **extra}


def msg(text: str = "hi") -> list[ChatMessage]:
    return [ChatMessage(role=Role.USER, content=text)]


async def test_default_policy_without_fallback_reports_failure(tmp_path: Path) -> None:
    c = await container(tmp_path, [p("a", "unavailable"), p("b")], {"mode": "default"})
    with pytest.raises(ProviderUnavailable):
        await c.inference.complete(ChatCommand(messages=msg(), model="shared-model"))
    assert CALLS == {"a": 1}  # no silent cross-provider fallback
    await c.aclose()


async def test_explicit_fallback_on_retryable_error(tmp_path: Path) -> None:
    c = await container(
        tmp_path,
        [p("a", "unavailable"), p("b")],
        {
            "mode": "default",
            "allow_cross_provider_fallback": True,
            "fallback_provider_ids": ["b"],
            "respect_health": False,
        },
    )
    response = await c.inference.complete(ChatCommand(messages=msg(), model="shared-model"))
    assert response.provider_id == "b"
    assert [a.outcome for a in response.route.attempts if a.outcome != "selected"] == [
        "failed",
        "succeeded",
    ]
    assert CALLS == {"a": 1, "b": 1}
    await c.aclose()


async def test_no_fallback_on_non_retryable_error(tmp_path: Path) -> None:
    c = await container(
        tmp_path,
        [p("a", "error"), p("b")],
        {"mode": "default", "allow_cross_provider_fallback": True, "fallback_provider_ids": ["b"]},
    )
    with pytest.raises(ProviderError):
        await c.inference.complete(ChatCommand(messages=msg(), model="shared-model"))
    assert CALLS == {"a": 1}
    await c.aclose()


async def test_stream_never_falls_back_after_output_started(tmp_path: Path) -> None:
    c = await container(
        tmp_path,
        [p("a", "midstream"), p("b")],
        {"mode": "default", "allow_cross_provider_fallback": True, "fallback_provider_ids": ["b"]},
    )
    events = [
        e async for e in c.inference.stream(ChatCommand(messages=msg(), model="shared-model"))
    ]
    kinds = [e.event for e in events]
    assert kinds[0] == StreamEventType.STARTED and kinds[-1] == StreamEventType.FAILED
    assert events[-1].data.partial is True  # type: ignore[union-attr]
    assert "b" not in CALLS
    await c.aclose()


async def test_health_down_provider_is_skipped_in_non_explicit_modes(tmp_path: Path) -> None:
    c = await container(
        tmp_path,
        [p("a", "unavailable"), p("b")],
        {
            "mode": "default",
            "allow_cross_provider_fallback": True,
            "fallback_provider_ids": ["b"],
            "respect_health": True,
        },
    )
    await c.health.check_all()
    response = await c.inference.complete(ChatCommand(messages=msg(), model="shared-model"))
    assert response.provider_id == "b"
    assert CALLS == {"b": 1}  # a was skipped without being called
    await c.aclose()


async def test_explicit_provider_is_honoured_and_never_falls_back(tmp_path: Path) -> None:
    c = await container(
        tmp_path,
        [p("a"), p("b", "unavailable")],
        {"mode": "default", "allow_cross_provider_fallback": True, "fallback_provider_ids": ["a"]},
    )
    with pytest.raises(ProviderUnavailable):
        await c.inference.complete(
            ChatCommand(messages=msg(), provider_id="b", model="shared-model")
        )
    assert CALLS == {"b": 1}
    await c.aclose()


async def test_sensitive_prompts_never_reach_external_providers(tmp_path: Path) -> None:
    c = await container(
        tmp_path,
        [p("a", locality="external"), p("b", locality="local")],
        {"mode": "capability_based", "allow_cross_provider_fallback": True},
    )
    with pytest.raises(PolicyViolation):
        await c.inference.complete(
            ChatCommand(
                messages=msg(),
                provider_id="a",
                model="shared-model",
                sensitivity=Sensitivity.SENSITIVE,
            )
        )
    response = await c.inference.complete(
        ChatCommand(messages=msg(), model="shared-model", sensitivity=Sensitivity.SENSITIVE)
    )
    assert response.provider_id == "b"
    assert "a" not in CALLS
    await c.aclose()


async def test_capability_based_routing_prefers_capable_local_healthy(tmp_path: Path) -> None:
    c = await container(
        tmp_path,
        [p("a", locality="external"), p("b", locality="local")],
        {"mode": "capability_based"},
    )
    response = await c.inference.complete(ChatCommand(messages=msg(), model="shared-model"))
    assert response.provider_id == "b"
    await c.aclose()


async def test_missing_capability_is_explicit(tmp_path: Path) -> None:
    c = await container(tmp_path, [p("a")], {"mode": "default"})
    image = ImagePart(media_type="image/png", data_base64="iVBORw0KGgo=")
    with pytest.raises(CapabilityNotSupported) as info:
        await c.inference.complete(
            ChatCommand(
                messages=[ChatMessage(role=Role.USER, content=[TextPart(text="describe"), image])],
                model="shared-model",
            )
        )
    assert info.value.details["missing"] == ["vision_input"]
    await c.aclose()


async def test_no_default_provider_is_a_clear_error(tmp_path: Path) -> None:
    raw = base_config(tmp_path, inference={})
    c = await build_container(parse_config(raw, {}))
    with pytest.raises(NoRouteAvailable, match="no default provider"):
        await c.inference.complete(ChatCommand(messages=msg()))
    await c.aclose()


async def test_service_level_cancellation_closes_stream(tmp_path: Path) -> None:
    raw = base_config(tmp_path)
    raw["providers"][0]["config"] = {"first_token_delay_ms": 1, "token_delay_ms": 50}
    c = await build_container(parse_config(raw, {}))
    cmd = ChatCommand(messages=msg("long answer please"), persist=True)
    events = []
    async for event in c.inference.stream(cmd):
        events.append(event)
        if event.event == StreamEventType.DELTA and len(events) == 3:
            assert await c.inference.cancel(cmd.request_id)
    assert events[-1].event == StreamEventType.FAILED
    assert events[-1].data.error.code == "request_cancelled"  # type: ignore[union-attr]
    assert c.inference.inflight() == []
    records = await c.metadata.recent_records()
    assert records[0].status.value == "cancelled"
    conversation = await c.metadata.get_conversation(cmd.conversation_id)  # type: ignore[arg-type]
    assert conversation is not None and conversation.messages[-1].metadata["status"] == "cancelled"
    await c.aclose()


async def test_closing_the_iterator_cancels_the_request(tmp_path: Path) -> None:
    raw = base_config(tmp_path)
    raw["providers"][0]["config"] = {"first_token_delay_ms": 1, "token_delay_ms": 50}
    c = await build_container(parse_config(raw, {}))
    stream = c.inference.stream(ChatCommand(messages=msg()))
    async for event in stream:
        if event.event == StreamEventType.DELTA:
            break
    await stream.aclose()  # what Starlette does when the HTTP client disconnects
    await asyncio.sleep(0.05)
    assert c.inference.inflight() == []
    assert (await c.metadata.recent_records())[0].status.value == "cancelled"
    await c.aclose()


async def test_completed_stream_records_completed_status(tmp_path: Path) -> None:
    c = await build_container(parse_config(base_config(tmp_path), {}))
    cmd = ChatCommand(messages=msg())
    events = [e async for e in c.inference.stream(cmd)]
    assert events[-1].event == StreamEventType.COMPLETED
    records = await c.metadata.recent_records()
    assert [r.status.value for r in records] == ["completed"]
    await c.aclose()


async def test_context_window_is_enforced(tmp_path: Path) -> None:
    raw = base_config(tmp_path)
    raw["providers"][0]["config"] = {"models": [{"name": "tiny", "context_window": 10}]}
    c = await build_container(parse_config(raw, {}))
    from omf.core.domain.errors import ContextLengthExceeded

    with pytest.raises(ContextLengthExceeded):
        await c.inference.complete(ChatCommand(messages=msg("word " * 200), model="tiny"))
    await c.aclose()


async def test_passive_health_marks_provider_down_after_threshold(tmp_path: Path) -> None:
    c = await container(tmp_path, [p("a")], {"mode": "default"})
    for expected in ("degraded", "degraded", "down"):
        c.health.record_failure("a", ProviderUnavailable("boom"))
        assert c.health.get("a").status.value == expected
    c.health.record_failure("a", ProviderError("bad request"))  # non-retryable: no health effect
    assert c.health.get("a").consecutive_failures == 3
    c.health.record_success("a", 5.0)
    assert c.health.get("a").status == HealthStatus.UP
    await c.aclose()
