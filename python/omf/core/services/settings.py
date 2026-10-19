"""Runtime-adjustable settings (defaults, routing, timeouts).

Values start from the configuration profile and can be overridden at runtime through
the API; overrides are validated and persisted in the metadata store so they survive
restarts. Security-relevant settings (auth mode, egress policy, CORS) are *not* runtime
adjustable — they are read-only here and can only change through the profile.
"""

from __future__ import annotations

from typing import Any

from pydantic import ConfigDict, Field, ValidationError

from omf.core.domain.base import DomainModel
from omf.core.domain.errors import ValidationFailed
from omf.core.domain.models import RoutingMode, RoutingPolicy
from omf.core.ports.metadata import SettingsRepository
from omf.core.services.provider_registry import ProviderRegistry

SETTINGS_KEY = "runtime_settings"


class RuntimeSettings(DomainModel):
    model_config = ConfigDict(extra="forbid")

    default_provider_id: str | None = None
    default_model: str | None = None
    routing: RoutingPolicy = Field(default_factory=RoutingPolicy)
    request_timeout_s: float = Field(default=300.0, gt=0, le=3600)
    """Upper bound for a whole request, across fallback attempts."""
    default_storage_id: str | None = None


class SettingsService:
    def __init__(
        self,
        repository: SettingsRepository,
        providers: ProviderRegistry,
        defaults: RuntimeSettings,
        *,
        storage_ids: list[str],
        read_only: dict[str, Any],
    ) -> None:
        self._repo = repository
        self._providers = providers
        self._defaults = defaults
        self._storage_ids = storage_ids
        self._read_only = read_only
        self._current = defaults

    async def load(self) -> RuntimeSettings:
        stored = await self._repo.get_setting(SETTINGS_KEY)
        if stored:
            try:
                merged = self._defaults.model_dump() | stored
                self._current = RuntimeSettings.model_validate(merged)
            except ValidationError:
                # Stored overrides no longer match the schema: fall back to the profile.
                self._current = self._defaults
        return self._current

    @property
    def current(self) -> RuntimeSettings:
        return self._current

    def routing_policy(self) -> RoutingPolicy:
        policy = self._current.routing
        if policy.default_provider_id is None and self._current.default_provider_id:
            policy = policy.model_copy(
                update={"default_provider_id": self._current.default_provider_id}
            )
        return policy

    def read_only(self) -> dict[str, Any]:
        return dict(self._read_only)

    async def update(self, changes: dict[str, Any]) -> RuntimeSettings:
        merged = self._current.model_dump() | changes
        try:
            candidate = RuntimeSettings.model_validate(merged)
        except ValidationError as exc:
            raise ValidationFailed(
                "invalid settings",
                details={
                    "errors": [
                        {"loc": list(e["loc"]), "msg": e["msg"]}
                        for e in exc.errors(include_url=False, include_input=False)
                    ]
                },
            ) from exc
        self.validate(candidate)
        overrides = {
            k: v
            for k, v in candidate.model_dump(mode="json").items()
            if v != self._defaults.model_dump(mode="json").get(k)
        }
        await self._repo.set_setting(SETTINGS_KEY, overrides)
        self._current = candidate
        return candidate

    def validate(self, s: RuntimeSettings) -> None:
        known = {p.id for p in self._providers.definitions()}
        referenced = [
            s.default_provider_id,
            s.routing.default_provider_id,
            *s.routing.preferred_provider_ids,
            *s.routing.fallback_provider_ids,
        ]
        unknown = sorted({p for p in referenced if p and p not in known})
        if unknown:
            raise ValidationFailed(
                "settings reference unknown providers", details={"unknown": unknown}
            )
        if s.default_storage_id and s.default_storage_id not in self._storage_ids:
            raise ValidationFailed("unknown storage provider", details={"id": s.default_storage_id})
        r = s.routing
        if r.mode == RoutingMode.EXPLICIT:
            raise ValidationFailed(
                "'explicit' is not a routing policy setting: pass provider_id in the request"
            )
        if r.mode == RoutingMode.PREFERRED_WITH_FALLBACK and not r.preferred_provider_ids:
            raise ValidationFailed("preferred_with_fallback routing needs preferred_provider_ids")
        if r.fallback_provider_ids and not r.allow_cross_provider_fallback:
            raise ValidationFailed(
                "fallback_provider_ids are set but allow_cross_provider_fallback is false; "
                "cross-provider fallback must be enabled explicitly"
            )
