"""Registry of artifact storage providers."""

from __future__ import annotations

import asyncio
import contextlib

from omf.core.domain.errors import OMFError, StorageProviderNotFound
from omf.core.domain.models import HealthStatus, StorageStatus
from omf.core.ports.storage import ArtifactStorageProvider


class StorageRegistry:
    def __init__(self, providers: list[ArtifactStorageProvider], default_id: str | None) -> None:
        self._providers = {p.storage_provider_id: p for p in providers}
        self.default_id = default_id

    def ids(self) -> list[str]:
        return sorted(self._providers)

    def get(self, storage_id: str | None = None) -> ArtifactStorageProvider:
        sid = storage_id or self.default_id
        if sid is None or sid not in self._providers:
            raise StorageProviderNotFound(
                f"storage provider {sid!r} is not configured", details={"configured": self.ids()}
            )
        return self._providers[sid]

    async def statuses(self) -> list[StorageStatus]:
        async def one(p: ArtifactStorageProvider) -> StorageStatus:
            try:
                return await p.status()
            except OMFError as exc:
                return StorageStatus(
                    storage_provider_id=p.storage_provider_id,
                    adapter_type=p.adapter_type,
                    status=HealthStatus.DOWN,
                    message=exc.message,
                )

        return list(await asyncio.gather(*(one(p) for p in self._providers.values())))

    async def aclose(self) -> None:
        for p in self._providers.values():
            with contextlib.suppress(Exception):
                await p.aclose()
