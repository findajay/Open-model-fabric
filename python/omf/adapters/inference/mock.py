"""Deterministic in-process inference adapter.

Lets contributors run, test and demo the whole platform without a GPU, model weights or
network access. It is a real adapter (it implements the full port, including the optional
``cancel`` operation) but it does not run a language model: responses are generated from
the prompt deterministically. The UI labels it as a mock.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from collections.abc import AsyncGenerator
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from omf.core.domain.capabilities import (
    Capability,
    CapabilityClaim,
    CapabilitySet,
    Evidence,
    Modality,
)
from omf.core.domain.errors import CapabilityNotSupported, ModelNotFound, ProviderUnavailable
from omf.core.domain.models import (
    AuthRequirement,
    DiscoveredModel,
    EmbeddingRequest,
    EmbeddingResult,
    GenerationChunk,
    GenerationResult,
    HealthStatus,
    InferenceRequest,
    ProviderCapabilities,
    ProviderDefinition,
    ProviderHealth,
    ProviderOperation,
    ResponseFormat,
    Role,
    TokenUsage,
    ToolCall,
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
            Capability.TOOL_CALLING,
        ]
    )
    dimensions: int = Field(default=64, ge=2, le=4096)
    """Vector size for models with the ``embeddings`` capability."""


def _default_models() -> list[MockModelConfig]:
    return [
        MockModelConfig(name="mock-chat"),
        MockModelConfig(
            name="mock-vision",
            capabilities=[Capability.CHAT, Capability.TEXT_GENERATION, Capability.VISION_INPUT],
        ),
        MockModelConfig(name="mock-embed", capabilities=[Capability.EMBEDDINGS]),
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
                        for c in [
                            *m.capabilities,
                            *([Capability.STREAMING] if Capability.CHAT in m.capabilities else []),
                        ]
                    )
                ).normalized(),
                capabilities_complete=True,
                details={"simulated": True},
            )
            for m in self._models.values()
        ]

    async def generate(self, request: InferenceRequest) -> GenerationResult:
        parts: list[str] = []
        calls = []
        finish = None
        usage = None
        async for chunk in self.stream_generate(request):
            parts.append(chunk.delta)
            calls.extend(chunk.tool_calls)
            finish = chunk.finish_reason or finish
            usage = chunk.usage or usage
        return GenerationResult(
            content="".join(parts), tool_calls=calls, finish_reason=finish, usage=usage
        )

    async def stream_generate(
        self, request: InferenceRequest
    ) -> AsyncGenerator[GenerationChunk, None]:
        self._check_outage()
        model = self._model(request.model, Capability.CHAT)
        calls = self._tool_calls(request, model)
        if calls:
            await asyncio.sleep(self._config.first_token_delay_ms / 1000)
            yield GenerationChunk(tool_calls=calls)
            prompt_tokens = sum(len(_tokens(m.text())) for m in request.messages)
            completion = sum(len(_tokens(c.arguments)) for c in calls)
            yield GenerationChunk(
                finish_reason="tool_calls",
                usage=TokenUsage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion,
                    total_tokens=prompt_tokens + completion,
                    source="provider",
                ),
            )
            return
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

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        self._check_outage()
        model = self._model(request.model, Capability.EMBEDDINGS)
        dims = request.dimensions or model.dimensions
        await asyncio.sleep(self._config.first_token_delay_ms / 1000)
        tokens = sum(len(_tokens(text)) for text in request.inputs)
        return EmbeddingResult(
            vectors=[_hashed_embedding(text, dims) for text in request.inputs],
            usage=TokenUsage(prompt_tokens=tokens, total_tokens=tokens, source="provider"),
        )

    # ------------------------------------------------------------------ internals

    def _model(self, name: str, capability: Capability) -> MockModelConfig:
        model = self._models.get(name)
        if model is None:
            raise ModelNotFound(
                f"mock model {name!r} does not exist",
                details={"available": sorted(self._models)},
            )
        if capability not in model.capabilities:
            raise CapabilityNotSupported(
                f"mock model {name!r} does not support {capability.value}",
                details={"model": name, "missing": [capability.value]},
            )
        return model

    def _check_outage(self) -> None:
        if self._config.simulate_outage:
            raise ProviderUnavailable(
                f"provider {self.provider_id!r} is unavailable (simulated)",
                details={"provider_id": self.provider_id},
            )

    def _tool_calls(self, request: InferenceRequest, model: MockModelConfig) -> list[ToolCall]:
        """Call a tool when tools are offered and the conversation ends with a user turn.

        Deterministic: the named tool (``tool_choice``) or the first one, with arguments
        filled from the schema's required properties.
        """
        if not request.tools or request.tool_choice == "none" or not request.messages:
            return []
        if Capability.TOOL_CALLING not in model.capabilities:
            raise CapabilityNotSupported(
                f"mock model {model.name!r} does not support tool_calling",
                details={"model": model.name, "missing": ["tool_calling"]},
            )
        if request.messages[-1].role != Role.USER:
            return []  # tool results came back: answer in text
        tool = next((t for t in request.tools if t.name == request.tool_choice), request.tools[0])
        prompt = request.messages[-1].text().strip()[:100]
        properties = tool.parameters.get("properties") or {}
        arguments = {
            name: _example_value(properties.get(name) or {}, prompt)
            for name in tool.parameters.get("required") or list(properties)[:1]
        }
        return [
            ToolCall(
                id=f"call_{request.request_id}_0",
                name=tool.name,
                arguments=json.dumps(arguments),
            )
        ]

    def _respond(self, request: InferenceRequest) -> str:
        if request.messages and request.messages[-1].role == Role.TOOL:
            results = [m for m in request.messages if m.role == Role.TOOL]
            lines = [f"**Mock response** from `{request.model}` using tool results:", ""]
            for m in results:
                name = request.tool_name_for(m.tool_call_id or "") or "tool"
                lines.append(f"- `{name}` returned: {m.text()[:200]}")
            return "\n".join(lines)
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


def _example_value(schema: dict[str, Any], prompt: str) -> Any:
    if schema.get("enum"):
        return schema["enum"][0]
    kind = schema.get("type")
    if kind in ("integer", "number"):
        return 1
    if kind == "boolean":
        return True
    if kind == "array":
        return []
    if kind == "object":
        return {}
    return prompt or "example"


def _hashed_embedding(text: str, dims: int) -> list[float]:
    """Deterministic bag-of-words feature hashing, L2-normalised.

    Texts that share words get similar vectors, which is enough to demo and test
    similarity search. It carries no semantics beyond word overlap.
    """
    vector = [0.0] * dims
    for word in re.findall(r"\w+", text.lower()):
        digest = hashlib.sha256(word.encode()).digest()
        index = int.from_bytes(digest[:4], "big") % dims
        vector[index] += 1.0 if digest[4] & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vector))
    return [round(v / norm, 6) for v in vector] if norm else vector


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
        ProviderOperation.EMBED,
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
