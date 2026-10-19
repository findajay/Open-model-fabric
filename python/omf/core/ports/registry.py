"""Adapter descriptors: how an adapter type declares itself to the provider registry.

Adapter types are registered explicitly by the composition root
(:mod:`omf.bootstrap.catalog`). Configuration can only *select* a registered adapter type
by id and supply schema-validated settings; it can never name a Python module, class or
code to execute (see ADR-0004).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from omf.core.domain.errors import ConfigurationError, ValidationFailed
from omf.core.domain.models import Locality, ProviderCapabilities, ProviderDefinition
from omf.core.ports.inference import InferenceProvider
from omf.core.ports.secrets import SecretValue
from omf.core.ports.telemetry import TelemetryProvider
from omf.core.security.egress import EgressPolicy

AdapterKind = Literal["inference", "compute", "storage"]


@dataclass(frozen=True)
class AdapterContext:
    """Services an adapter factory may use. Secrets are resolved lazily, by reference."""

    resolve_secret: Callable[[str], SecretValue]
    egress: EgressPolicy
    telemetry: TelemetryProvider


@dataclass(frozen=True)
class InferenceAdapterDescriptor:
    adapter_type: str
    display_name: str
    version: str
    description: str
    config_model: type[BaseModel]
    capabilities: ProviderCapabilities
    factory: Callable[[ProviderDefinition, Any, AdapterContext], InferenceProvider]
    default_locality: Locality = Locality.LOCAL
    endpoint_fields: tuple[str, ...] = ()
    """Config fields holding URLs; they are checked against the egress policy."""
    secret_fields: tuple[str, ...] = ()
    """Config fields holding secret *references*."""
    future_compatible_with: tuple[str, ...] = field(default_factory=tuple)

    def parse_config(self, raw: dict[str, Any]) -> BaseModel:
        try:
            return self.config_model.model_validate(raw)
        except ValidationError as exc:
            raise ValidationFailed(
                f"invalid configuration for adapter {self.adapter_type!r}",
                details={
                    "errors": [
                        {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                        for e in exc.errors(include_url=False, include_input=False)
                    ]
                },
            ) from exc

    def describe(self) -> dict[str, Any]:
        return {
            "adapter_type": self.adapter_type,
            "kind": "inference",
            "display_name": self.display_name,
            "version": self.version,
            "description": self.description,
            "capabilities": self.capabilities.model_dump(mode="json"),
            "default_locality": self.default_locality.value,
            "config_schema": self.config_model.model_json_schema(),
            "endpoint_fields": list(self.endpoint_fields),
            "secret_fields": list(self.secret_fields),
            "future_compatible_with": list(self.future_compatible_with),
        }


class AdapterCatalog:
    """Explicit registry of inference adapter types available in this build."""

    def __init__(self, descriptors: list[InferenceAdapterDescriptor]) -> None:
        self._by_type: dict[str, InferenceAdapterDescriptor] = {}
        for d in descriptors:
            if d.adapter_type in self._by_type:
                raise ConfigurationError(f"duplicate adapter type {d.adapter_type!r}")
            self._by_type[d.adapter_type] = d

    def get(self, adapter_type: str) -> InferenceAdapterDescriptor:
        try:
            return self._by_type[adapter_type]
        except KeyError:
            raise ValidationFailed(
                f"unknown adapter type {adapter_type!r}",
                details={"available": sorted(self._by_type)},
            ) from None

    def all(self) -> list[InferenceAdapterDescriptor]:
        return [self._by_type[k] for k in sorted(self._by_type)]
