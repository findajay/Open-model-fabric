"""OpenAI-compatible chat-completions adapter.

One adapter covers every engine that implements the OpenAI ``/v1/chat/completions`` and
``/v1/models`` protocol: vLLM, Hugging Face Text Generation Inference (Messages API),
llama.cpp ``llama-server``, LM Studio, LocalAI, SGLang, hosted OpenAI-compatible
services, and custom servers that follow the protocol. Engine differences are handled by
configuration (``stream_usage``, ``list_models``), not by separate adapters.

Model capabilities: ``/v1/models`` carries no capability metadata. Listed models are
therefore marked ``chat`` *declared* by the adapter (the operator configured a chat
endpoint); everything else (vision, tools, JSON mode) must be declared in the provider's
``models`` section or verified with the probe.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from omf.adapters.inference._http import HttpEndpoint, read_error_body
from omf.core.domain.capabilities import Capability, CapabilitySet, Modality
from omf.core.domain.errors import OMFError, ProviderAuthenticationFailed, ProviderError
from omf.core.domain.models import (
    AuthRequirement,
    ChatMessage,
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
from omf.core.ports.secrets import SecretRefStr

ADAPTER_TYPE = "openai_compatible"
ADAPTER_VERSION = "1.0.0"
_FORBIDDEN_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "host",
    "api-key",
    "x-api-key",
}


class OpenAICompatibleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(
        description="Base URL including the version prefix, e.g. http://vllm:8000/v1"
    )
    api_key_ref: SecretRefStr | None = None
    extra_headers: dict[str, str] = Field(default_factory=dict)
    """Non-secret headers. Credentials must use ``api_key_ref``."""
    stream_usage: bool = True
    """Send ``stream_options.include_usage`` (supported by OpenAI, vLLM, llama.cpp)."""
    list_models: bool = True
    """Some servers lack ``GET /models``; then only the declared ``models`` are used."""
    verify_tls: bool = True

    @field_validator("extra_headers")
    @classmethod
    def _no_secret_headers(cls, v: dict[str, str]) -> dict[str, str]:
        bad = sorted(h for h in v if h.lower() in _FORBIDDEN_HEADERS)
        if bad:
            raise ValueError(f"headers {bad} may carry credentials; use api_key_ref instead")
        return v


def _content(message: ChatMessage) -> str | list[dict[str, Any]]:
    if isinstance(message.content, str):
        return message.content
    parts: list[dict[str, Any]] = []
    for part in message.content:
        if part.type == "text":
            parts.append({"type": "text", "text": part.text})
        else:
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{part.media_type};base64,{part.data_base64}"},
                }
            )
    return parts


def _usage(raw: dict[str, Any] | None) -> TokenUsage | None:
    if not raw:
        return None
    return TokenUsage(
        prompt_tokens=raw.get("prompt_tokens"),
        completion_tokens=raw.get("completion_tokens"),
        total_tokens=raw.get("total_tokens"),
        source="provider",
    )


class OpenAICompatibleInferenceAdapter(InferenceProvider):
    def __init__(
        self,
        provider_id: str,
        config: OpenAICompatibleConfig,
        endpoint: HttpEndpoint,
        declared_models: list[str],
    ) -> None:
        super().__init__(provider_id)
        self._config = config
        self._http = endpoint
        self._declared = declared_models

    def get_capabilities(self) -> ProviderCapabilities:
        return CAPABILITIES

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ discovery

    async def list_models(self) -> list[DiscoveredModel]:
        if not self._config.list_models:
            return [self._discovered(name, {}) for name in self._declared]
        await self._http.ensure_egress()
        try:
            response = await self._http.client.get("/models")
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, ValueError):
                raise ProviderError(f"provider {self.provider_id!r} returned invalid JSON") from exc
            self._http.raise_for(exc)
        return [self._discovered(str(m["id"]), m) for m in payload.get("data", []) if "id" in m]

    def _discovered(self, name: str, raw: dict[str, Any]) -> DiscoveredModel:
        context = raw.get("max_model_len") or raw.get("context_length") or raw.get("context_window")
        return DiscoveredModel(
            name=name,
            context_window=int(context) if isinstance(context, int) and context > 0 else None,
            capabilities=CapabilitySet.declared(
                [Capability.CHAT, Capability.TEXT_GENERATION], f"adapter:{ADAPTER_TYPE}"
            ),
            details={k: raw[k] for k in ("owned_by", "created", "root") if k in raw},
        )

    # ------------------------------------------------------------------ inference

    def _body(self, request: InferenceRequest, *, stream: bool) -> dict[str, Any]:
        p = request.params
        body: dict[str, Any] = {
            "model": request.model,
            "messages": [{"role": m.role.value, "content": _content(m)} for m in request.messages],
            "stream": stream,
        }
        for key, value in (
            ("temperature", p.temperature),
            ("max_tokens", p.max_tokens),
            ("top_p", p.top_p),
            ("stop", p.stop),
            ("seed", p.seed),
        ):
            if value is not None:
                body[key] = value
        if p.response_format == ResponseFormat.JSON:
            body["response_format"] = {"type": "json_object"}
        if stream and self._config.stream_usage:
            body["stream_options"] = {"include_usage": True}
        return body

    async def generate(self, request: InferenceRequest) -> GenerationResult:
        await self._http.ensure_egress()
        try:
            response = await self._http.client.post(
                "/chat/completions", json=self._body(request, stream=False)
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            self._http.raise_for(exc)
        except ValueError as exc:
            raise ProviderError(f"provider {self.provider_id!r} returned invalid JSON") from exc
        try:
            choice = payload["choices"][0]
            content = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                f"provider {self.provider_id!r} returned an unexpected payload"
            ) from exc
        return GenerationResult(
            content=content,
            finish_reason=choice.get("finish_reason"),
            usage=_usage(payload.get("usage")),
        )

    async def stream_generate(
        self, request: InferenceRequest
    ) -> AsyncGenerator[GenerationChunk, None]:
        await self._http.ensure_egress()
        finish: str | None = None
        usage: TokenUsage | None = None
        try:
            async with self._http.client.stream(
                "POST",
                "/chat/completions",
                json=self._body(request, stream=True),
                headers={"Accept": "text/event-stream"},
            ) as response:
                if response.status_code >= 400:
                    raise self._http.status_error(
                        response.status_code, await read_error_body(response)
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue  # comments, event names, keep-alives
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except ValueError as exc:
                        raise ProviderError(
                            f"provider {self.provider_id!r} sent malformed SSE data"
                        ) from exc
                    if "error" in event:
                        err = event["error"]
                        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                        raise ProviderError(
                            f"provider {self.provider_id!r} stream error: {msg[:300]}"
                        )
                    usage = _usage(event.get("usage")) or usage
                    for choice in event.get("choices") or []:
                        delta = (choice.get("delta") or {}).get("content")
                        if delta:
                            yield GenerationChunk(delta=delta)
                        if choice.get("finish_reason"):
                            finish = choice["finish_reason"]
        except OMFError:
            raise
        except httpx.HTTPError as exc:
            self._http.raise_for(exc)
        yield GenerationChunk(finish_reason=finish or "stop", usage=usage)

    async def health_check(self) -> ProviderHealth:
        started = time.perf_counter()
        try:
            await self._http.ensure_egress()
            # Without /models, any non-5xx answer from the base URL proves reachability.
            response = await self._http.client.get("/models" if self._config.list_models else "")
            latency = round((time.perf_counter() - started) * 1000, 2)
            failed = response.status_code >= (400 if self._config.list_models else 500)
            if failed:
                err = self._http.status_error(response.status_code, response.text[:2000])
                return ProviderHealth(
                    provider_id=self.provider_id,
                    status=HealthStatus.DOWN,
                    latency_ms=latency,
                    message=err.message,
                    error_code=err.code,
                    checked_at=utcnow(),
                )
            return ProviderHealth(
                provider_id=self.provider_id,
                status=HealthStatus.UP,
                latency_ms=latency,
                checked_at=utcnow(),
            )
        except (httpx.HTTPError, OMFError) as exc:
            err = self._http.translate(exc)
            return ProviderHealth(
                provider_id=self.provider_id,
                status=HealthStatus.DOWN,
                message=err.message,
                error_code=err.code,
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
    ],
    modalities=[Modality.TEXT, Modality.IMAGE],
    streaming=True,
    health_check=True,
    authentication=AuthRequirement.OPTIONAL_API_KEY,
    model_capabilities_source="declared",
)


def create(
    definition: ProviderDefinition,
    config: OpenAICompatibleConfig,
    ctx: AdapterContext,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> OpenAICompatibleInferenceAdapter:
    headers = dict(config.extra_headers)
    preflight: OMFError | None = None
    if config.api_key_ref:
        # Resolved here, on the backend only; never returned by any API. A missing secret
        # does not prevent startup: the provider reports an authentication error instead.
        try:
            headers["Authorization"] = f"Bearer {ctx.resolve_secret(config.api_key_ref).reveal()}"
        except OMFError as exc:
            preflight = ProviderAuthenticationFailed(
                f"provider {definition.id!r}: secret {config.api_key_ref} is not available",
                details={
                    "provider_id": definition.id,
                    "reference": config.api_key_ref,
                    "cause": exc.code,
                },
            )
    endpoint = HttpEndpoint(
        provider_id=definition.id,
        base_url=config.base_url,
        timeouts=definition.timeouts,
        egress=ctx.egress,
        headers=headers,
        verify_tls=config.verify_tls,
        transport=transport,
        preflight_error=preflight,
    )
    return OpenAICompatibleInferenceAdapter(
        definition.id, config, endpoint, [m.name for m in definition.models]
    )
