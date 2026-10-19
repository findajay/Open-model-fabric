"""Registry of compute environments (where inference servers run)."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from omf.core.domain.errors import ComputeEnvironmentNotFound, OMFError
from omf.core.domain.models import ComputeEnvironment, ComputeState, utcnow
from omf.core.ports.compute import ComputeProvider

log = logging.getLogger(__name__)


class ComputeRegistry:
    def __init__(self, providers: list[ComputeProvider]) -> None:
        self._providers = {p.environment_id: p for p in providers}
        self._cache: dict[str, ComputeEnvironment] = {}

    def ids(self) -> list[str]:
        return sorted(self._providers)

    def provider(self, environment_id: str) -> ComputeProvider:
        try:
            return self._providers[environment_id]
        except KeyError:
            raise ComputeEnvironmentNotFound(
                f"compute environment {environment_id!r} is not configured",
                details={"configured": self.ids()},
            ) from None

    async def describe(self, environment_id: str, *, refresh: bool = False) -> ComputeEnvironment:
        provider = self.provider(environment_id)
        if not refresh and environment_id in self._cache:
            return self._cache[environment_id]
        try:
            env = await provider.describe_environment()
        except OMFError as exc:
            # An environment that cannot be described is reported as UNKNOWN rather than
            # failing the whole listing (e.g. an offline Colab notebook).
            env = ComputeEnvironment(
                id=environment_id,
                kind="unknown",
                display_name=environment_id,
                state=ComputeState.UNKNOWN,
                notes=[f"{exc.code}: {exc.message}"],
                checked_at=utcnow(),
            )
        self._cache[environment_id] = env
        return env

    async def describe_all(self, *, refresh: bool = False) -> list[ComputeEnvironment]:
        return list(await asyncio.gather(*(self.describe(i, refresh=refresh) for i in self.ids())))

    async def aclose(self) -> None:
        for p in self._providers.values():
            with contextlib.suppress(Exception):
                await p.aclose()
