"""Registry of compute environments (where inference servers run)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from omf.core.domain.errors import ComputeEnvironmentNotFound, OMFError, ValidationFailed
from omf.core.domain.models import ComputeEnvironment, ComputeOperation, ComputeState, utcnow
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

    async def operate(
        self,
        environment_id: str,
        operation: ComputeOperation,
        *,
        wait: bool = False,
        timeout_s: float = 600.0,
        poll_s: float = 2.0,
    ) -> ComputeEnvironment:
        """Run a lifecycle operation; optionally wait until the environment settles."""
        provider = self.provider(environment_id)
        actions = {
            ComputeOperation.PROVISION: provider.provision,
            ComputeOperation.START: provider.start,
            ComputeOperation.STOP: provider.stop,
            ComputeOperation.DEPROVISION: provider.deprovision,
        }
        if operation not in actions:
            raise ValidationFailed(f"{operation.value} is not a lifecycle operation")
        env = await actions[operation]()
        targets = {
            ComputeOperation.START: {ComputeState.ONLINE},
            ComputeOperation.STOP: {ComputeState.OFFLINE},
            ComputeOperation.DEPROVISION: {ComputeState.OFFLINE},
            ComputeOperation.PROVISION: {ComputeState.OFFLINE, ComputeState.ONLINE},
        }[operation]
        if wait and env.state not in targets:
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                await asyncio.sleep(poll_s)
                if await provider.get_status() in targets:
                    break
            env = await self.describe(environment_id, refresh=True)
        self._cache[environment_id] = env
        return env

    async def aclose(self) -> None:
        for p in self._providers.values():
            with contextlib.suppress(Exception):
                await p.aclose()
