"""Ollama adapter (native ``/api`` protocol).

Why a dedicated adapter when Ollama also exposes ``/v1`` (OpenAI-compatible)? The native
API reports model metadata the OpenAI protocol cannot: ``/api/show`` returns the model's
capability list (``completion``, ``vision``, ``tools``, ``embedding``…), context length,
family and quantization. That makes Ollama capabilities ``provider_reported`` instead of
merely declared. Users who prefer the ``/v1`` endpoint can configure Ollama through the
``openai_compatible`` adapter instead.

Streaming uses newline-delimited JSON; this adapter normalises it to ``GenerationChunk``.
Closing the stream aborts generation on the Ollama server.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from omf.adapters.inference._http import HttpEndpoint, check_vectors, read_error_body
from omf.core.domain.capabilities import (
    Capability,
    CapabilityClaim,
    CapabilitySet,
    Evidence,
    Modality,
)
from omf.core.domain.errors import OMFError, ProviderError
from omf.core.domain.models import (
    AuthRequirement,
    ChatMessage,
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
    TokenUsage,
    utcnow,
)
from omf.core.ports.inference import InferenceProvider
from omf.core.ports.registry import AdapterContext

ADAPTER_TYPE = "ollama"
ADAPTER_VERSION = "1.0.0"

# Ollama capability names -> OMF capabilities
_CAPABILITY_MAP: dict[str, list[Capability]] = {
    "completion": [Capability.CHAT, Capability.TEXT_GENERATION],
    "vision": [Capability.VISION_INPUT],
    "tools": [Capability.TOOL_CALLING],
    "embedding": [Capability.EMBEDDINGS],
}


class OllamaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = "http://localhost:11434"
    keep_alive: str | None = Field(default=None, description="e.g. '5m'; Ollama default if unset")
    num_ctx: int | None = Field(default=None, gt=0, description="Override the context size")
    fetch_model_details: bool = True
    """Call ``/api/show`` per model to learn capabilities and context length."""


def _message(m: ChatMessage) -> dict[str, Any]:
    out: dict[str, Any] = {"role": m.role.value, "content": m.text()}
    images = m.images()
    if images:
        out["images"] = [img.data_base64 for img in images]
    return out


def _usage(payload: dict[str, Any]) -> TokenUsage | None:
    prompt, completion = payload.get("prompt_eval_count"), payload.get("eval_count")
    if prompt is None and completion is None:
        return None
    total = (prompt or 0) + (completion or 0)
    return TokenUsage(
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=total, source="provider"
    )


class OllamaInferenceAdapter(InferenceProvider):
    def __init__(self, provider_id: str, config: OllamaConfig, endpoint: HttpEndpoint) -> None:
        super().__init__(provider_id)
        self._config = config
        self._http = endpoint

    def get_capabilities(self) -> ProviderCapabilities:
        return CAPABILITIES

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ discovery

    async def _get_json(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        await self._http.ensure_egress()
        try:
            response = await self._http.client.request(method, path, json=body)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            self._http.raise_for(exc)
        except ValueError as exc:
            raise ProviderError(f"provider {self.provider_id!r} returned invalid JSON") from exc

    async def list_models(self) -> list[DiscoveredModel]:
        tags = await self._get_json("GET", "/api/tags")
        entries = [m for m in tags.get("models", []) if m.get("name") or m.get("model")]
        semaphore = asyncio.Semaphore(4)

        async def describe(entry: dict[str, Any]) -> DiscoveredModel:
            name = entry.get("name") or entry["model"]
            details = entry.get("details") or {}
            show: dict[str, Any] = {}
            if self._config.fetch_model_details:
                async with semaphore:
                    try:
                        show = await self._get_json("POST", "/api/show", {"model": name})
                    except OMFError:
                        show = {}
            return self._discovered(name, details, show)

        return list(await asyncio.gather(*(describe(e) for e in entries)))

    def _discovered(
        self, name: str, details: dict[str, Any], show: dict[str, Any]
    ) -> DiscoveredModel:
        now = utcnow()
        reported = show.get("capabilities")
        claims: list[CapabilityClaim] = []
        complete = isinstance(reported, list)
        if isinstance(reported, list):
            for cap in reported:
                for omf_cap in _CAPABILITY_MAP.get(str(cap), []):
                    claims.append(
                        CapabilityClaim(
                            capability=omf_cap,
                            evidence=Evidence.PROVIDER_REPORTED,
                            source="ollama:/api/show",
                            checked_at=now,
                        )
                    )
        else:
            # Older Ollama versions do not report capabilities; declare only basic chat.
            claims += [
                CapabilityClaim(
                    capability=c, evidence=Evidence.DECLARED, source=f"adapter:{ADAPTER_TYPE}"
                )
                for c in (Capability.CHAT, Capability.TEXT_GENERATION)
            ]
        if any(c.capability == Capability.CHAT for c in claims):
            # Ollama's `format: json` is an engine feature available to completion models.
            claims.append(
                CapabilityClaim(
                    capability=Capability.STRUCTURED_OUTPUT,
                    evidence=Evidence.DECLARED,
                    source=f"adapter:{ADAPTER_TYPE}(format=json)",
                )
            )
        context = None
        for key, value in (show.get("model_info") or {}).items():
            if key.endswith(".context_length") and isinstance(value, int):
                context = value
                break
        show_details = show.get("details") or {}
        return DiscoveredModel(
            name=name,
            family=details.get("family") or show_details.get("family"),
            context_window=self._config.num_ctx or context,
            quantization=details.get("quantization_level")
            or show_details.get("quantization_level"),
            capabilities=CapabilitySet(claims=tuple(claims)).normalized(),
            capabilities_complete=complete,
            details={
                k: v
                for k, v in {
                    "parameter_size": details.get("parameter_size")
                    or show_details.get("parameter_size"),
                    "format": details.get("format") or show_details.get("format"),
                    "model_context_length": context,
                }.items()
                if v is not None
            },
        )

    # ------------------------------------------------------------------ inference

    def _body(self, request: InferenceRequest, *, stream: bool) -> dict[str, Any]:
        p = request.params
        options = {
            k: v
            for k, v in {
                "temperature": p.temperature,
                "num_predict": p.max_tokens,
                "top_p": p.top_p,
                "stop": p.stop,
                "seed": p.seed,
                "num_ctx": self._config.num_ctx,
            }.items()
            if v is not None
        }
        body: dict[str, Any] = {
            "model": request.model,
            "messages": [_message(m) for m in request.messages],
            "stream": stream,
        }
        if options:
            body["options"] = options
        if p.response_format == ResponseFormat.JSON:
            body["format"] = "json"
        if self._config.keep_alive:
            body["keep_alive"] = self._config.keep_alive
        return body

    async def generate(self, request: InferenceRequest) -> GenerationResult:
        payload = await self._get_json("POST", "/api/chat", self._body(request, stream=False))
        if "error" in payload:
            raise ProviderError(f"provider {self.provider_id!r}: {str(payload['error'])[:300]}")
        return GenerationResult(
            content=(payload.get("message") or {}).get("content", ""),
            finish_reason=payload.get("done_reason") or "stop",
            usage=_usage(payload),
        )

    async def stream_generate(
        self, request: InferenceRequest
    ) -> AsyncGenerator[GenerationChunk, None]:
        await self._http.ensure_egress()
        final: dict[str, Any] | None = None
        try:
            async with self._http.client.stream(
                "POST", "/api/chat", json=self._body(request, stream=True)
            ) as response:
                if response.status_code >= 400:
                    raise self._http.status_error(
                        response.status_code, await read_error_body(response)
                    )
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError as exc:
                        raise ProviderError(
                            f"provider {self.provider_id!r} sent malformed NDJSON"
                        ) from exc
                    if "error" in event:
                        raise ProviderError(
                            f"provider {self.provider_id!r} stream error: "
                            f"{str(event['error'])[:300]}"
                        )
                    delta = (event.get("message") or {}).get("content")
                    if delta:
                        yield GenerationChunk(delta=delta)
                    if event.get("done"):
                        final = event
                        break
        except OMFError:
            raise
        except httpx.HTTPError as exc:
            self._http.raise_for(exc)
        if final is None:
            raise ProviderError(
                f"provider {self.provider_id!r} closed the stream before completion"
            )
        yield GenerationChunk(finish_reason=final.get("done_reason") or "stop", usage=_usage(final))

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        body: dict[str, Any] = {"model": request.model, "input": request.inputs}
        if request.dimensions is not None:
            body["dimensions"] = request.dimensions
        if self._config.keep_alive:
            body["keep_alive"] = self._config.keep_alive
        payload = await self._get_json("POST", "/api/embed", body)
        if "error" in payload:
            raise ProviderError(f"provider {self.provider_id!r}: {str(payload['error'])[:300]}")
        return EmbeddingResult(
            vectors=check_vectors(self.provider_id, payload.get("embeddings"), request),
            usage=_usage(payload),
        )

    async def health_check(self) -> ProviderHealth:
        started = time.perf_counter()
        try:
            payload = await self._get_json("GET", "/api/version")
        except OMFError as exc:
            return ProviderHealth(
                provider_id=self.provider_id,
                status=HealthStatus.DOWN,
                message=exc.message,
                error_code=exc.code,
                checked_at=utcnow(),
            )
        return ProviderHealth(
            provider_id=self.provider_id,
            status=HealthStatus.UP,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            message=f"ollama {payload.get('version', 'unknown')}",
            checked_at=utcnow(),
        )


CAPABILITIES = ProviderCapabilities(
    adapter_type=ADAPTER_TYPE,
    adapter_version=ADAPTER_VERSION,
    operations=[
        ProviderOperation.LIST_MODELS,
        ProviderOperation.GET_CAPABILITIES,
        ProviderOperation.GENERATE,
        ProviderOperation.STREAM_GENERATE,
        ProviderOperation.HEALTH_CHECK,
        ProviderOperation.EMBED,
    ],
    modalities=[Modality.TEXT, Modality.IMAGE],
    streaming=True,
    health_check=True,
    authentication=AuthRequirement.NONE,
    model_capabilities_source="provider_metadata",
)


def create(
    definition: ProviderDefinition,
    config: OllamaConfig,
    ctx: AdapterContext,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> OllamaInferenceAdapter:
    endpoint = HttpEndpoint(
        provider_id=definition.id,
        base_url=config.base_url,
        timeouts=definition.timeouts,
        egress=ctx.egress,
        transport=transport,
    )
    return OllamaInferenceAdapter(definition.id, config, endpoint)
