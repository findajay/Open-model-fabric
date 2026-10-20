"""Model registry: catalog definitions + per-provider deployments + capability evidence.

Capability claims for a deployment are merged from, strongest evidence first:

1. ``verified``          — results of :meth:`ModelRegistryService.verify` (persisted).
2. ``provider_reported`` — what the engine reports (e.g. Ollama ``/api/show``).
3. ``declared``          — the provider definition's ``models`` entries, the catalog entry
                           for the mapped model, and adapter defaults (e.g. "a model listed
                           by a chat-completions endpoint is declared chat-capable").

Anything without a claim is unsupported. The registry never invents claims.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from omf.core.domain.capabilities import (
    Capability,
    CapabilityClaim,
    CapabilitySet,
    Evidence,
)
from omf.core.domain.errors import ModelNotFound, OMFError, ProviderUnavailable
from omf.core.domain.models import (
    ChatMessage,
    DeploymentState,
    DiscoveredModel,
    EmbeddingRequest,
    GenerationParams,
    InferenceRequest,
    ModelDefinition,
    ModelDeployment,
    ProviderDefinition,
    ResponseFormat,
    Role,
    ToolDefinition,
    new_id,
    utcnow,
)
from omf.core.ports.metadata import ModelRegistrationRepository, SettingsRepository
from omf.core.services.provider_registry import ProviderRegistry

VERIFICATIONS_KEY = "capability_verifications"


@dataclass
class _Discovery:
    fetched_at: float = 0.0
    models: list[DiscoveredModel] = field(default_factory=list)
    error: OMFError | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ModelRegistryService:
    def __init__(
        self,
        providers: ProviderRegistry,
        repository: ModelRegistrationRepository,
        settings: SettingsRepository,
        *,
        discovery_ttl_s: float = 30.0,
    ) -> None:
        self._providers = providers
        self._repo = repository
        self._settings = settings
        self._ttl = discovery_ttl_s
        self._discovery: dict[str, _Discovery] = {}

    # ------------------------------------------------------------------ catalog

    async def seed(self, catalog: list[ModelDefinition]) -> None:
        """Upsert catalog entries from the configuration profile (idempotent)."""
        for model in catalog:
            await self._repo.upsert_model(model.model_copy(update={"source": "catalog"}))

    async def register(self, model: ModelDefinition) -> ModelDefinition:
        model = model.model_copy(update={"source": "api"})
        await self._repo.upsert_model(model)
        return model

    async def definitions(self) -> list[ModelDefinition]:
        return sorted(await self._repo.list_models(), key=lambda m: m.id)

    async def definition(self, model_id: str) -> ModelDefinition:
        model = await self._repo.get_model(model_id)
        if model is None:
            raise ModelNotFound(
                f"model {model_id!r} is not registered", details={"model_id": model_id}
            )
        return model

    async def delete(self, model_id: str) -> None:
        if not await self._repo.delete_model(model_id):
            raise ModelNotFound(f"model {model_id!r} is not registered")

    # ------------------------------------------------------------------ discovery

    async def _discover(self, provider_id: str, *, refresh: bool) -> _Discovery:
        state = self._discovery.setdefault(provider_id, _Discovery())
        async with state.lock:
            fresh = time.monotonic() - state.fetched_at < self._ttl and state.fetched_at > 0
            if fresh and not refresh:
                return state
            try:
                async with self._providers.lease(provider_id) as adapter:
                    state.models = await adapter.list_models()
                state.error = None
            except OMFError as exc:
                state.models = []
                state.error = exc
            state.fetched_at = time.monotonic()
            return state

    def invalidate(self, provider_id: str | None = None) -> None:
        if provider_id is None:
            self._discovery.clear()
        else:
            self._discovery.pop(provider_id, None)

    async def deployments(
        self, provider_id: str | None = None, *, refresh: bool = False
    ) -> list[ModelDeployment]:
        catalog = await self._repo.list_models()
        verifications = await self._settings.get_setting(VERIFICATIONS_KEY) or {}
        ids = [provider_id] if provider_id else self._providers.enabled_ids()
        results = await asyncio.gather(
            *(self._provider_deployments(pid, catalog, verifications, refresh) for pid in ids)
        )
        return [d for group in results for d in group]

    async def discovery_error(self, provider_id: str) -> OMFError | None:
        state = self._discovery.get(provider_id)
        return state.error if state else None

    async def _provider_deployments(
        self,
        provider_id: str,
        catalog: list[ModelDefinition],
        verifications: dict[str, Any],
        refresh: bool,
    ) -> list[ModelDeployment]:
        definition = self._providers.get(provider_id)
        if not definition.enabled:
            return []
        discovery = await self._discover(provider_id, refresh=refresh)
        default_caps = self._providers.effective(provider_id).descriptor.capabilities
        declared = {m.name: m for m in definition.models}
        by_alias = {alias: m for m in catalog for alias in [m.id, *m.provider_aliases]}
        out: dict[str, ModelDeployment] = {}

        for found in discovery.models:
            out[found.name] = self._build(
                definition,
                found.name,
                found,
                declared.get(found.name),
                by_alias,
                DeploymentState.AVAILABLE,
                verifications,
                adapter_default_streaming=default_caps.streaming,
            )
        for name, decl in declared.items():
            if name in out:
                continue
            state = DeploymentState.UNKNOWN if discovery.error else DeploymentState.UNAVAILABLE
            out[name] = self._build(
                definition,
                name,
                None,
                decl,
                by_alias,
                state,
                verifications,
                adapter_default_streaming=default_caps.streaming,
            )
        return sorted(out.values(), key=lambda d: d.provider_model)

    def _build(
        self,
        provider: ProviderDefinition,
        name: str,
        found: DiscoveredModel | None,
        declared: Any,
        by_alias: dict[str, ModelDefinition],
        state: DeploymentState,
        verifications: dict[str, Any],
        *,
        adapter_default_streaming: bool,
    ) -> ModelDeployment:
        model_id = declared.model_id if declared and declared.model_id else None
        catalog_entry = by_alias.get(model_id or name)
        if catalog_entry is not None:
            model_id = catalog_entry.id
        caps = found.capabilities if found else CapabilitySet()
        extra = CapabilitySet()
        if declared and declared.capabilities:
            extra = extra.merge(
                CapabilitySet.declared(declared.capabilities, f"provider:{provider.id}")
            )
        if catalog_entry is not None:
            extra = extra.merge(
                CapabilitySet(
                    claims=tuple(
                        c.model_copy(
                            update={
                                "evidence": Evidence.DECLARED,
                                "source": f"catalog:{catalog_entry.id}",
                            }
                        )
                        for c in catalog_entry.capabilities.claims
                    )
                )
            )
        if found is not None and found.capabilities_complete:
            # The engine is authoritative: declarations cannot add what it denies.
            extra = CapabilitySet(
                claims=tuple(c for c in extra.claims if caps.supports(c.capability))
            )
        if extra.claims and all(c.evidence == Evidence.DECLARED for c in caps.claims):
            # Only adapter guesses so far (e.g. "a listed OpenAI model is a chat model"):
            # an explicit operator or catalog declaration replaces them, so an embedding
            # model declared as [embeddings] does not also claim chat.
            caps = extra
        else:
            caps = caps.merge(extra)
        if adapter_default_streaming and caps.supports(Capability.CHAT):
            # Streaming is a protocol feature of the adapter; still only "declared".
            caps = caps.merge(CapabilitySet.declared([Capability.STREAMING], "adapter"))
        deployment_key = f"{provider.id}/{name}"
        verified = [
            CapabilityClaim.model_validate(c) for c in verifications.get(deployment_key, [])
        ]
        if verified:
            caps = caps.merge(CapabilitySet(claims=tuple(verified)))
        return ModelDeployment(
            provider_id=provider.id,
            provider_model=name,
            model_id=model_id,
            compute_environment_id=provider.compute_environment_id,
            state=state,
            capabilities=caps,
            context_window=(found.context_window if found else None)
            or (declared.context_window if declared else None)
            or (catalog_entry.context_window if catalog_entry else None),
            quantization=(found.quantization if found else None)
            or (declared.quantization if declared else None),
            family=(found.family if found else None)
            or (catalog_entry.family if catalog_entry else None),
            details=found.details if found else {},
            discovered_at=utcnow() if found else None,
        )

    # ------------------------------------------------------------------ resolution

    async def resolve(self, provider_id: str, model: str) -> ModelDeployment:
        """Find ``model`` (provider-native name or catalog id) on ``provider_id``."""
        deployments = await self.deployments(provider_id)
        by_name = {d.provider_model: d for d in deployments}
        # Engines with ``name:tag`` models (Ollama) treat an untagged name as
        # ``name:latest``; only that tag is implied, never another explicit one.
        names = [model] if ":" in model else [model, f"{model}:latest"]
        matches = [by_name[n] for n in names if n in by_name]
        for d in matches:
            if d.state == DeploymentState.AVAILABLE:
                return d
        if matches:
            return matches[0]
        for d in deployments:
            if d.model_id == model and d.state == DeploymentState.AVAILABLE:
                return d
        for d in deployments:
            if d.model_id == model:
                return d
        error = await self.discovery_error(provider_id)
        if error is not None:
            raise ProviderUnavailable(
                f"cannot resolve model {model!r}: provider {provider_id!r} is unreachable "
                f"({error.message})",
                details={"provider_id": provider_id, "model": model, "cause": error.code},
            )
        raise ModelNotFound(
            f"model {model!r} is not served by provider {provider_id!r}",
            details={
                "provider_id": provider_id,
                "model": model,
                "available": [d.provider_model for d in deployments],
            },
        )

    # ------------------------------------------------------------------ verification

    async def verify(self, provider_id: str, model: str) -> dict[str, Any]:
        """Exercise claimed capabilities and record the ones that work as ``verified``."""
        deployment = await self.resolve(provider_id, model)
        checks: list[dict[str, Any]] = []
        verified: list[CapabilityClaim] = []
        now = utcnow()

        def ok(cap: Capability, detail: str) -> None:
            checks.append({"capability": cap.value, "result": "passed", "detail": detail})
            verified.append(
                CapabilityClaim(
                    capability=cap, evidence=Evidence.VERIFIED, source="omf:probe", checked_at=now
                )
            )

        def fail(cap: Capability, detail: str) -> None:
            checks.append({"capability": cap.value, "result": "failed", "detail": detail})

        prompt = [ChatMessage(role=Role.USER, content="Reply with the single word: OK")]
        params = GenerationParams(max_tokens=16, temperature=0)
        caps = deployment.capabilities
        # Embedding-only models are not sent a chat probe (it would always fail).
        probe_chat = caps.supports(Capability.CHAT) or not caps.supports(Capability.EMBEDDINGS)
        async with self._providers.lease(provider_id) as adapter:
            req = InferenceRequest(
                request_id=new_id("probe"),
                model=deployment.provider_model,
                messages=prompt,
                params=params,
            )
            if caps.supports(Capability.EMBEDDINGS):
                try:
                    embedded = await adapter.embed(
                        EmbeddingRequest(
                            request_id=new_id("probe"),
                            model=deployment.provider_model,
                            inputs=["probe one", "probe two"],
                        )
                    )
                    ok(
                        Capability.EMBEDDINGS,
                        f"returned 2 vectors of {len(embedded.vectors[0])} dimensions",
                    )
                except OMFError as exc:
                    fail(Capability.EMBEDDINGS, f"{exc.code}: {exc.message}")
            if probe_chat:
                try:
                    result = await adapter.generate(req)
                    if result.content.strip():
                        ok(Capability.CHAT, "non-streaming chat completion returned text")
                        ok(Capability.TEXT_GENERATION, "model produced text output")
                    else:
                        fail(Capability.CHAT, "empty completion")
                except OMFError as exc:
                    fail(Capability.CHAT, f"{exc.code}: {exc.message}")

            if deployment.capabilities.supports(Capability.STREAMING):
                chunks = 0
                try:
                    async for chunk in adapter.stream_generate(
                        req.model_copy(update={"request_id": new_id("probe")})
                    ):
                        if chunk.delta:
                            chunks += 1
                    if chunks:
                        ok(Capability.STREAMING, f"received {chunks} streamed deltas")
                    else:
                        fail(Capability.STREAMING, "stream produced no deltas")
                except OMFError as exc:
                    fail(Capability.STREAMING, f"{exc.code}: {exc.message}")

            if deployment.capabilities.supports(Capability.STRUCTURED_OUTPUT):
                json_req = InferenceRequest(
                    request_id=new_id("probe"),
                    model=deployment.provider_model,
                    messages=[
                        ChatMessage(role=Role.USER, content='Return a JSON object {"ok": true}.')
                    ],
                    params=GenerationParams(
                        max_tokens=32, temperature=0, response_format=ResponseFormat.JSON
                    ),
                )
                try:
                    result = await adapter.generate(json_req)
                    json.loads(result.content)
                    ok(Capability.STRUCTURED_OUTPUT, "JSON mode returned parseable JSON")
                except (OMFError, ValueError) as exc:
                    fail(Capability.STRUCTURED_OUTPUT, f"JSON mode check failed: {exc}")

            if caps.supports(Capability.TOOL_CALLING):
                tool = ToolDefinition(
                    name="get_weather",
                    description="Get the current weather for a city",
                    parameters={
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                )
                tool_req = InferenceRequest(
                    request_id=new_id("probe"),
                    model=deployment.provider_model,
                    messages=[
                        ChatMessage(
                            role=Role.USER,
                            content="What is the weather in Paris? Use the get_weather tool.",
                        )
                    ],
                    params=GenerationParams(max_tokens=128, temperature=0),
                    tools=[tool],
                )
                try:
                    result = await adapter.generate(tool_req)
                    call = next((c for c in result.tool_calls if c.name == tool.name), None)
                    if call is None:
                        fail(Capability.TOOL_CALLING, "model answered without calling the tool")
                    elif not isinstance(json.loads(call.arguments), dict):
                        fail(Capability.TOOL_CALLING, "tool arguments are not a JSON object")
                    else:
                        ok(Capability.TOOL_CALLING, f"called {tool.name} with {call.arguments}")
                except (OMFError, ValueError) as exc:
                    fail(Capability.TOOL_CALLING, f"tool call check failed: {exc}")

        for cap in deployment.capabilities.claims:
            if cap.capability.value not in {c["capability"] for c in checks}:
                checks.append(
                    {
                        "capability": cap.capability.value,
                        "result": "not_probed",
                        "detail": f"remains {cap.evidence.value}; "
                        "no automatic probe for this capability",
                    }
                )

        all_verifications = await self._settings.get_setting(VERIFICATIONS_KEY) or {}
        all_verifications[deployment.id] = [c.model_dump(mode="json") for c in verified]
        await self._settings.set_setting(VERIFICATIONS_KEY, all_verifications)
        updated = await self.resolve(provider_id, deployment.provider_model)
        return {
            "deployment": updated.model_dump(mode="json"),
            "deployment_id": updated.id,
            "checks": checks,
        }
