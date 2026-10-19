"""Composition root: builds every service from an :class:`AppConfig`.

This is the only module that knows about both the core and concrete adapters.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any, BinaryIO

from omf.adapters.metadata.sql import SqlMetadataStore
from omf.adapters.secrets.env import EnvSecretProvider, FileSecretProvider
from omf.adapters.telemetry.log_telemetry import CompositeTelemetry, LoggingTelemetry
from omf.bootstrap.catalog import build_compute, build_storage, inference_catalog
from omf.bootstrap.config import AppConfig
from omf.core.domain.errors import OMFError, StorageError
from omf.core.domain.models import HealthStatus, StorageArtifact, StorageStatus
from omf.core.ports.registry import AdapterCatalog, AdapterContext
from omf.core.ports.storage import ArtifactStorageProvider
from omf.core.ports.telemetry import TelemetryProvider
from omf.core.services.audit import AuditService
from omf.core.services.benchmarks import BenchmarkService
from omf.core.services.compute import ComputeRegistry
from omf.core.services.health import HealthMonitor
from omf.core.services.inference import InferenceService
from omf.core.services.model_registry import ModelRegistryService
from omf.core.services.provider_registry import ProviderRegistry
from omf.core.services.routing import Router
from omf.core.services.secrets import SecretResolver
from omf.core.services.settings import RuntimeSettings, SettingsService
from omf.core.services.storage import StorageRegistry

log = logging.getLogger(__name__)


class UnavailableStorage(ArtifactStorageProvider):
    """Stands in for a storage provider that failed to initialise (graceful degradation)."""

    def __init__(self, storage_provider_id: str, adapter_type: str, error: OMFError) -> None:
        super().__init__(storage_provider_id)
        self.adapter_type = adapter_type
        self._error = error

    def _fail(self) -> StorageError:
        return StorageError(
            f"storage {self.storage_provider_id!r} is unavailable: {self._error.message}",
            details={"cause": self._error.code},
        )

    async def put(
        self,
        key: str,
        source: BinaryIO,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> StorageArtifact:
        raise self._fail()

    async def get(self, key: str, destination: BinaryIO) -> StorageArtifact:
        raise self._fail()

    async def list(self, prefix: str = "") -> list[StorageArtifact]:
        raise self._fail()

    async def delete(self, key: str) -> None:
        raise self._fail()

    async def get_metadata(self, key: str) -> StorageArtifact:
        raise self._fail()

    async def verify_checksum(self, key: str, expected_sha256: str | None = None) -> bool:
        raise self._fail()

    async def status(self) -> StorageStatus:
        return StorageStatus(
            storage_provider_id=self.storage_provider_id,
            adapter_type=self.adapter_type,
            status=HealthStatus.DOWN,
            message=self._error.message,
            details={"error_code": self._error.code},
        )


@dataclass
class Container:
    config: AppConfig
    telemetry: TelemetryProvider
    logging_telemetry: LoggingTelemetry
    secrets: SecretResolver
    metadata: SqlMetadataStore
    catalog: AdapterCatalog
    providers: ProviderRegistry
    health: HealthMonitor
    models: ModelRegistryService
    settings: SettingsService
    router: Router
    inference: InferenceService
    compute: ComputeRegistry
    storage: StorageRegistry
    benchmarks: BenchmarkService
    audit: AuditService
    extras: dict[str, Any] = field(default_factory=dict)

    async def start(self, *, background: bool = True) -> None:
        if background:
            self.health.start()

    async def aclose(self) -> None:
        await self.benchmarks.shutdown()
        await self.health.stop()
        await self.providers.aclose()
        await self.compute.aclose()
        await self.storage.aclose()
        await self.metadata.aclose()
        with contextlib.suppress(Exception):
            self.telemetry.shutdown()


def build_telemetry(config: AppConfig) -> tuple[TelemetryProvider, LoggingTelemetry]:
    logging_telemetry = LoggingTelemetry()
    providers: list[TelemetryProvider] = [logging_telemetry]
    if "otlp" in config.telemetry.exporters:
        from omf.adapters.telemetry.otel import OpenTelemetryTelemetry

        providers.append(
            OpenTelemetryTelemetry(
                service_name=config.telemetry.service_name, endpoint=config.telemetry.otlp_endpoint
            )
        )
    return CompositeTelemetry(providers), logging_telemetry


async def build_container(
    config: AppConfig,
    *,
    catalog: AdapterCatalog | None = None,
    environ: dict[str, str] | None = None,
) -> Container:
    telemetry, logging_telemetry = build_telemetry(config)
    refs = config.secret_references()
    secrets = SecretResolver(
        [
            EnvSecretProvider(refs, environ),
            FileSecretProvider(config.security.secret_dirs, refs),
        ]
    )
    db_url = config.metadata.url or secrets.resolve(str(config.metadata.url_ref)).reveal()
    metadata = SqlMetadataStore(db_url)
    await metadata.initialize()

    context = AdapterContext(
        resolve_secret=secrets.resolve, egress=config.security.egress, telemetry=telemetry
    )
    catalog = catalog or inference_catalog()
    providers = ProviderRegistry(catalog, context, metadata, metadata)
    await providers.load(config.providers)

    compute_providers = []
    for c in config.compute:
        compute_providers.append(build_compute(c.id, c.adapter, c.config, context))
    compute = ComputeRegistry(compute_providers)

    storage_providers: list[ArtifactStorageProvider] = []
    for s in config.storage:
        try:
            storage_providers.append(build_storage(s.id, s.adapter, s.config, context))
        except OMFError as exc:
            log.warning("storage %s unavailable: %s", s.id, exc.message)
            storage_providers.append(UnavailableStorage(s.id, s.adapter, exc))
    default_storage = next(
        (s.id for s in config.storage if s.default),
        config.storage[0].id if config.storage else None,
    )
    storage = StorageRegistry(storage_providers, default_storage)

    health = HealthMonitor(
        providers,
        telemetry,
        interval_s=config.health.interval_s,
        failure_threshold=config.health.failure_threshold,
    )
    models = ModelRegistryService(
        providers, metadata, metadata, discovery_ttl_s=config.inference.discovery_ttl_s
    )
    await models.seed([m.to_domain() for m in config.models])

    inf = config.inference
    defaults = RuntimeSettings(
        default_provider_id=inf.default_provider,
        default_model=inf.default_model,
        routing=inf.routing,
        request_timeout_s=inf.request_timeout_s,
        default_storage_id=default_storage,
    )
    settings = SettingsService(
        metadata,
        providers,
        defaults,
        storage_ids=storage.ids(),
        read_only={
            "profile": config.profile,
            "environment": config.environment,
            "auth_mode": config.auth.mode,
            "cors_allow_origins": config.server.cors_allow_origins,
            "max_request_bytes": config.server.max_request_bytes,
            "max_upload_bytes": config.server.max_upload_bytes,
            "egress": config.security.egress.model_dump(),
            "log_prompt_content": config.security.log_prompt_content,
            "telemetry_exporters": config.telemetry.exporters,
        },
    )
    await settings.load()
    router = Router(providers, models, health)
    inference = InferenceService(providers, router, health, settings, metadata, metadata, telemetry)
    benchmarks = BenchmarkService(metadata, inference, providers, models, compute)
    audit = AuditService(metadata)
    return Container(
        config=config,
        telemetry=telemetry,
        logging_telemetry=logging_telemetry,
        secrets=secrets,
        metadata=metadata,
        catalog=catalog,
        providers=providers,
        health=health,
        models=models,
        settings=settings,
        router=router,
        inference=inference,
        compute=compute,
        storage=storage,
        benchmarks=benchmarks,
        audit=audit,
    )
