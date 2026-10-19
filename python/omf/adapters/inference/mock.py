"""Deterministic in-process inference adapter.

Lets contributors run, test and demo the whole platform without a GPU, model weights or
network access. It is a real adapter (it implements the full port, including the optional
``cancel`` operation) but it does not run a language model: responses are generated from
the prompt deterministically. The UI labels it as a mock.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omf.core.domain.capabilities import (
    Capability,
    CapabilityClaim,
    CapabilitySet,
    Evidence,
    Modality,
)
from omf.core.domain.errors import ModelNotFound, ProviderUnavailable
from omf.core.domain.models import (
    AuthRequirement,
    DiscoveredModel,
    GenerationChunk,
    GenerationResult,
    HealthStatus,
    InferenceRequest,
    ProviderCapabilities,
    ProviderDefinition,
    ProviderHealth,
    ProviderOperation,
    ResponseFormat,
    TokenUsage,
    utcnow,
)
from omf.core.ports.inference import InferenceProvider
from omf.core.ports.registry import AdapterContext

ADAPTER_TYPE = "mock"
ADAPTER_VERSION = "1.0.0"


class MockModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    family: str = "mock"
    context_window: int = 8192
    capabilities: list[Capability] = Field(
        default_factory=lambda: [
            Capability.CHAT,
            Capability.TEXT_GENERATION,
            Capability.STRUCTURED_OUTPUT,
        ]
    )


def _default_models() -> list[MockModelConfig]:
    return [
        MockModelConfig(name="mock-chat"),
        MockModelConfig(
            name="mock-vision",
            capabilities=[Capability.CHAT, Capability.TEXT_GENERATION, Capability.VISION_INPUT],
        ),
    ]


class MockConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[MockModelConfig] = Field(default_factory=_default_models)
    first_token_delay_ms: float = Field(default=40.0, ge=0, le=60_000)
    token_delay_ms: float = Field(default=12.0, ge=0, le=10_000)
    simulate_outage: bool = False
    """Make every call fail with ``provider_unavailable`` (for resilience testing)."""
    response_style: Literal["echo", "markdown"] = "markdown"


def _tokens(text: str) -> list[str]:
    out: list[str] = []
    word = ""
    for ch in text:
        word += ch
        if ch in " \n":
            out.append(word)
            word = ""
    if word:
        out.append(word)
    return out


class MockInferenceAdapter(InferenceProvider):
    def __init__(self, provider_id: str, config: MockConfig) -> None:
        super().__init__(provider_id)
        self._config = config
        self._models = {m.name: m for m in config.models}
        self._cancelled: set[str] = set()

    def get_capabilities(self) -> ProviderCapabilities:
        return CAPABILITIES

    async def list_models(self) -> list[DiscoveredModel]:
        self._check_outage()
        return [
            DiscoveredModel(
                name=m.name,
                family=m.family,
                context_window=m.context_window,
                quantization="none",
                capabilities=CapabilitySet(
                    claims=tuple(
                        CapabilityClaim(
                            capability=c,
                            evidence=Evidence.PROVIDER_REPORTED,
                            source="mock",
                            checked_at=utcnow(),
                        )
                        for c in [*m.capabilities, Capability.STREAMING]
                    )
                ).normalized(),
                capabilities_complete=True,
                details={"simulated": True},
            )
            for m in self._models.values()
        ]

    async def generate(self, request: InferenceRequest) -> GenerationResult:
        parts: list[str] = []
        finish = None
        usage = None
        async for chunk in self.stream_generate(request):
            parts.append(chunk.delta)
            finish = chunk.finish_reason or finish
            usage = chunk.usage or usage
        return GenerationResult(content="".join(parts), finish_reason=finish, usage=usage)

    async def stream_generate(
        self, request: InferenceRequest
    ) -> AsyncGenerator[GenerationChunk, None]:
        self._check_outage()
        model = self._models.get(request.model)
        if model is None:
            raise ModelNotFound(
                f"mock model {request.model!r} does not exist",
                details={"available": sorted(self._models)},
            )
        text = self._respond(request)
        tokens = _tokens(text)
        if request.params.max_tokens is not None:
            tokens = tokens[: request.params.max_tokens]
        finish = "length" if len(tokens) < len(_tokens(text)) else "stop"
        prompt_tokens = sum(len(_tokens(m.text())) for m in request.messages)
        await asyncio.sleep(self._config.first_token_delay_ms / 1000)
        emitted = 0
        for token in tokens:
            if request.request_id in self._cancelled:
                self._cancelled.discard(request.request_id)
                finish = "cancelled"
                break
            yield GenerationChunk(delta=token)
            emitted += 1
            await asyncio.sleep(self._config.token_delay_ms / 1000)
        yield GenerationChunk(
            finish_reason=finish,
            usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=emitted,
                total_tokens=prompt_tokens + emitted,
                source="provider",
            ),
        )

    async def health_check(self) -> ProviderHealth:
        started = time.perf_counter()
        if self._config.simulate_outage:
            return ProviderHealth(
                provider_id=self.provider_id,
                status=HealthStatus.DOWN,
                message="simulated outage",
                error_code="provider_unavailable",
                checked_at=utcnow(),
            )
        return ProviderHealth(
            provider_id=self.provider_id,
            status=HealthStatus.UP,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            message="in-process mock",
            checked_at=utcnow(),
        )

    async def cancel(self, request_id: str) -> None:
        self._cancelled.add(request_id)

    # ------------------------------------------------------------------ internals

    def _check_outage(self) -> None:
        if self._config.simulate_outage:
            raise ProviderUnavailable(
                f"provider {self.provider_id!r} is unavailable (simulated)",
                details={"provider_id": self.provider_id},
            )

    def _respond(self, request: InferenceRequest) -> str:
        last = next((m for m in reversed(request.messages) if m.role.value == "user"), None)
        prompt = last.text().strip() if last else ""
        images = len(last.images()) if last else 0
        if request.params.response_format == ResponseFormat.JSON:
            return json.dumps({"model": request.model, "echo": prompt[:200], "images": images})
        if self._config.response_style == "echo":
            return f"[{request.model}] {prompt}"
        lines = [
            f"**Mock response** from `{request.model}` (provider `{self.provider_id}`).",
            "",
            f"You said: _{prompt[:500]}_" if prompt else "You sent an empty message.",
        ]
        if images:
            lines += ["", f"Received {images} image(s) as vision input."]
        lines += [
            "",
            "This adapter does not run a model; it streams a deterministic reply so the "
            "platform can be developed without a GPU.",
            "",
            "```python",
            "print('hello from the mock provider')",
            "```",
        ]
        return "\n".join(lines)


CAPABILITIES = ProviderCapabilities(
    adapter_type=ADAPTER_TYPE,
    adapter_version=ADAPTER_VERSION,
    operations=[
        ProviderOperation.LIST_MODELS,
        ProviderOperation.GET_CAPABILITIES,
        ProviderOperation.GENERATE,
        ProviderOperation.STREAM_GENERATE,
        ProviderOperation.HEALTH_CHECK,
        ProviderOperation.CANCEL,
    ],
    modalities=[Modality.TEXT, Modality.IMAGE],
    streaming=True,
    health_check=True,
    authentication=AuthRequirement.NONE,
    model_capabilities_source="provider_metadata",
)


def create(
    definition: ProviderDefinition, config: MockConfig, _ctx: AdapterContext
) -> MockInferenceAdapter:
    return MockInferenceAdapter(definition.id, config)
