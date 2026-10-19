"""Runtime registry of configured inference providers.

* Providers come from two origins: ``config`` (the YAML profile; immutable at runtime
  except for the ``enabled`` flag) and ``runtime`` (added through the API and persisted
  in the metadata store).
* Configuration changes are validated (adapter schema, egress policy, secret reference
  syntax) and applied without restarting the process: a new adapter instance is built and
  swapped in; the old instance is closed once in-flight requests release it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from omf.core.domain.errors import (
    Conflict,
    OMFError,
    ProviderDisabled,
    ProviderNotFound,
    ValidationFailed,
)
from omf.core.domain.models import (
    HealthStatus,
    Locality,
    ProviderCapabilities,
    ProviderDefinition,
    ProviderHealth,
    utcnow,
)
from omf.core.ports.inference import InferenceProvider
from omf.core.ports.metadata import ProviderConfigRepository, SettingsRepository
from omf.core.ports.registry import AdapterCatalog, AdapterContext, InferenceAdapterDescriptor
from omf.core.ports.secrets import SecretRef

OVERRIDES_KEY = "provider_overrides"


@dataclass
class _Slot:
    definition: ProviderDefinition
    adapter: InferenceProvider | None  # None when disabled
    leases: int = 0


@dataclass(frozen=True)
class EffectiveProvider:
    definition: ProviderDefinition
    locality: Locality
    allow_sensitive: bool
    descriptor: InferenceAdapterDescriptor


class ProviderTestResult(dict[str, Any]):
    pass


class ProviderRegistry:
    def __init__(
        self,
        catalog: AdapterCatalog,
        context: AdapterContext,
        repository: ProviderConfigRepository,
        settings: SettingsRepository,
    ) -> None:
        self._catalog = catalog
        self._context = context
        self._repo = repository
        self._settings = settings
        self._slots: dict[str, _Slot] = {}
        self._retired: list[_Slot] = []
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ loading

    async def load(self, config_providers: list[ProviderDefinition]) -> None:
        overrides: dict[str, dict[str, Any]] = await self._settings.get_setting(OVERRIDES_KEY) or {}
        definitions: dict[str, ProviderDefinition] = {}
        for d in config_providers:
            d = d.model_copy(update={"origin": "config"})
            self._validate_static(d)
            if d.id in overrides and "enabled" in overrides[d.id]:
                d = d.model_copy(update={"enabled": bool(overrides[d.id]["enabled"])})
            definitions[d.id] = d
        for d in await self._repo.list_providers():
            if d.id in definitions:
                # A config provider wins over a stale runtime entry with the same id.
                continue
            definitions[d.id] = d.model_copy(update={"origin": "runtime"})
        async with self._lock:
            for d in definitions.values():
                self._slots[d.id] = _Slot(
                    definition=d, adapter=self._build(d) if d.enabled else None
                )

    async def aclose(self) -> None:
        async with self._lock:
            slots = list(self._slots.values()) + self._retired
            self._slots.clear()
            self._retired.clear()
        for slot in slots:
            if slot.adapter is not None:
                await slot.adapter.aclose()

    # ------------------------------------------------------------------ queries

    def catalog(self) -> AdapterCatalog:
        return self._catalog

    def definitions(self) -> list[ProviderDefinition]:
        return [s.definition for s in sorted(self._slots.values(), key=lambda s: s.definition.id)]

    def get(self, provider_id: str) -> ProviderDefinition:
        slot = self._slots.get(provider_id)
        if slot is None:
            raise ProviderNotFound(
                f"provider {provider_id!r} is not configured",
                details={"provider_id": provider_id, "configured": sorted(self._slots)},
            )
        return slot.definition

    def effective(self, provider_id: str) -> EffectiveProvider:
        d = self.get(provider_id)
        descriptor = self._catalog.get(d.adapter_type)
        locality = d.locality or descriptor.default_locality
        allow_sensitive = (
            d.allow_sensitive if d.allow_sensitive is not None else (locality == Locality.LOCAL)
        )
        return EffectiveProvider(d, locality, allow_sensitive, descriptor)

    def capabilities(self, provider_id: str) -> ProviderCapabilities:
        slot = self._slots.get(provider_id)
        if slot is not None and slot.adapter is not None:
            return slot.adapter.get_capabilities()
        return self._catalog.get(self.get(provider_id).adapter_type).capabilities

    def enabled_ids(self) -> list[str]:
        return [d.id for d in self.definitions() if d.enabled]

    @asynccontextmanager
    async def lease(self, provider_id: str) -> AsyncIterator[InferenceProvider]:
        """Borrow the provider's adapter; reconfiguration waits for leases to close it."""
        slot = self._slots.get(provider_id)
        if slot is None:
            self.get(provider_id)  # raises ProviderNotFound
            raise AssertionError("unreachable")
        if slot.adapter is None:
            raise ProviderDisabled(
                f"provider {provider_id!r} is disabled", details={"provider_id": provider_id}
            )
        slot.leases += 1
        try:
            yield slot.adapter
        finally:
            slot.leases -= 1
            if slot in self._retired and slot.leases == 0:
                self._retired.remove(slot)
                await slot.adapter.aclose()

    def adapters(self) -> list[tuple[ProviderDefinition, InferenceProvider | None]]:
        return [(s.definition, s.adapter) for s in self._slots.values()]

    # ------------------------------------------------------------------ mutation

    async def validate(
        self, definition: ProviderDefinition, *, resolve_dns: bool = True
    ) -> list[str]:
        """Validate a definition. Returns non-fatal warnings; raises on errors."""
        descriptor = self._validate_static(definition)
        warnings: list[str] = []
        config = descriptor.parse_config(definition.config)
        for field in descriptor.endpoint_fields:
            url = getattr(config, field, None)
            if url and resolve_dns:
                try:
                    await self._context.egress.check_url(str(url))
                except OMFError as exc:
                    if exc.retryable:  # unresolvable host — allowed, but warn
                        warnings.append(str(exc))
                    else:
                        raise
        for field in descriptor.secret_fields:
            ref = getattr(config, field, None)
            if ref:
                try:
                    self._context.resolve_secret(str(ref))
                except OMFError:
                    warnings.append(f"secret reference {ref} is not currently resolvable")
        return warnings

    async def create(self, definition: ProviderDefinition) -> list[str]:
        if definition.id in self._slots:
            raise Conflict(f"provider {definition.id!r} already exists")
        definition = definition.model_copy(
            update={"origin": "runtime", "created_at": utcnow(), "updated_at": utcnow()}
        )
        warnings = await self.validate(definition)
        await self._repo.upsert_provider(definition)
        async with self._lock:
            adapter = self._build(definition) if definition.enabled else None
            self._slots[definition.id] = _Slot(definition=definition, adapter=adapter)
        return warnings

    async def update(self, provider_id: str, changes: dict[str, Any]) -> list[str]:
        current = self.get(provider_id)
        allowed_for_config = {"enabled"}
        if current.origin == "config" and set(changes) - allowed_for_config:
            raise ValidationFailed(
                "providers defined in the configuration profile can only be enabled or "
                "disabled at runtime; edit the profile to change other fields",
                details={"provider_id": provider_id, "fields": sorted(changes)},
            )
        if "id" in changes and changes["id"] != provider_id:
            raise ValidationFailed("provider id cannot be changed")
        merged = current.model_dump()
        merged.update(changes)
        merged["updated_at"] = utcnow()
        updated = ProviderDefinition.model_validate(merged)
        warnings = await self.validate(updated)
        if current.origin == "config":
            overrides = await self._settings.get_setting(OVERRIDES_KEY) or {}
            overrides.setdefault(provider_id, {})["enabled"] = updated.enabled
            await self._settings.set_setting(OVERRIDES_KEY, overrides)
        else:
            await self._repo.upsert_provider(updated)
        await self._swap(updated)
        return warnings

    async def delete(self, provider_id: str) -> None:
        current = self.get(provider_id)
        if current.origin == "config":
            raise ValidationFailed(
                "providers defined in the configuration profile cannot be deleted at runtime; "
                "disable it instead"
            )
        await self._repo.delete_provider(provider_id)
        async with self._lock:
            slot = self._slots.pop(provider_id)
            await self._retire(slot)

    async def test(self, definition: ProviderDefinition) -> dict[str, Any]:
        """Instantiate a throw-away adapter and probe it. Never persists anything."""
        started = time.perf_counter()
        warnings = await self.validate(definition)
        adapter = self._build(definition)
        try:
            health = await adapter.health_check()
            models: list[str] = []
            error: dict[str, Any] | None = None
            if health.status in (HealthStatus.UP, HealthStatus.DEGRADED):
                try:
                    models = [m.name for m in await adapter.list_models()]
                except OMFError as exc:
                    error = {"code": exc.code, "message": exc.message}
            return {
                "provider_id": definition.id,
                "ok": health.status == HealthStatus.UP and error is None,
                "health": health.model_dump(mode="json"),
                "models": models,
                "capabilities": adapter.get_capabilities().model_dump(mode="json"),
                "warnings": warnings,
                "error": error,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        finally:
            await adapter.aclose()

    # ------------------------------------------------------------------ helpers

    def _validate_static(self, definition: ProviderDefinition) -> InferenceAdapterDescriptor:
        descriptor = self._catalog.get(definition.adapter_type)
        config = descriptor.parse_config(definition.config)
        for field in descriptor.endpoint_fields:
            url = getattr(config, field, None)
            if url:
                self._context.egress.check_url_syntax(str(url))
        for field in descriptor.secret_fields:
            ref = getattr(config, field, None)
            if ref:
                SecretRef.parse(str(ref))
        return descriptor

    def _build(self, definition: ProviderDefinition) -> InferenceProvider:
        descriptor = self._catalog.get(definition.adapter_type)
        config = descriptor.parse_config(definition.config)
        return descriptor.factory(definition, config, self._context)

    async def _swap(self, definition: ProviderDefinition) -> None:
        async with self._lock:
            old = self._slots.get(definition.id)
            adapter = self._build(definition) if definition.enabled else None
            self._slots[definition.id] = _Slot(definition=definition, adapter=adapter)
            if old is not None:
                await self._retire(old)

    async def _retire(self, slot: _Slot) -> None:
        if slot.adapter is None:
            return
        if slot.leases == 0:
            await slot.adapter.aclose()
        else:
            self._retired.append(slot)


def disabled_health(provider_id: str) -> ProviderHealth:
    return ProviderHealth(
        provider_id=provider_id, status=HealthStatus.DISABLED, checked_at=utcnow()
    )
