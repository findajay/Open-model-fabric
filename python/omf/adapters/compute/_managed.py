"""Base class for compute adapters that can change infrastructure.

Safety rules shared by every lifecycle-capable adapter (Docker, Kubernetes, cloud VMs):

1. **Opt-in.** Lifecycle operations (provision/start/stop/deprovision) are refused with
   ``lifecycle_disabled`` unless the environment's configuration sets
   ``allow_lifecycle: true``. Read-only operations always work.
2. **Ownership.** Adapters tag what they create (``omf-managed`` / ``omf-environment``
   labels or tags) and refuse to modify or delete resources without those markers. An
   existing resource with the configured name but without the markers is reported as a
   conflict, never adopted silently.
3. **One operation at a time** per environment (an asyncio lock), so double clicks or
   concurrent API calls cannot race.
4. **Bounded waiting.** Operations return once the backend accepted them; callers can ask
   to wait for the target state up to ``operation_timeout_s``.

Subclasses implement ``_inspect`` (current state, read only) and the ``_do_*`` methods.
"""

from __future__ import annotations

import asyncio
import time
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from omf.core.domain.errors import LifecycleDisabled, OMFError
from omf.core.domain.models import (
    LIFECYCLE_OPERATIONS,
    ComputeEnvironment,
    ComputeOperation,
    ComputeResources,
    ComputeState,
    EndpointInfo,
    utcnow,
)
from omf.core.ports.compute import ComputeProvider
from omf.core.security.egress import EgressPolicy

OWNER_LABEL = "omf-managed"
ENVIRONMENT_LABEL = "omf-environment"


class ManagedComputeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = None
    allow_lifecycle: bool = False
    """Allow Open Model Fabric to provision/start/stop/deprovision this environment."""
    operation_timeout_s: float = Field(default=600.0, gt=0, le=7200)
    endpoint_port: int | None = Field(default=None, ge=1, le=65535)
    """Port of the inference server running in the environment (for the endpoint URL)."""
    endpoint_scheme: str = Field(default="http", pattern=r"^https?$")
    health_path: str = "/"
    probe_endpoint: bool = True
    """Probe the endpoint's reachability (subject to the egress policy)."""


@dataclass
class Inspection:
    """Read-only snapshot of the environment, produced by ``_inspect``."""

    state: ComputeState
    exists: bool
    resources: ComputeResources | None = None
    endpoint_host: str | None = None
    notes: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


class ManagedComputeAdapter(ComputeProvider):
    kind: str = "managed"
    ephemeral: bool = False
    #: lifecycle operations this adapter implements (subset of LIFECYCLE_OPERATIONS)
    implemented: tuple[ComputeOperation, ...] = LIFECYCLE_OPERATIONS

    def __init__(
        self,
        environment_id: str,
        config: ManagedComputeConfig,
        egress: EgressPolicy,
        *,
        probe_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(environment_id)
        self.config = config
        self._egress = egress
        self._lock = asyncio.Lock()
        self._probe_transport = probe_transport

    # ------------------------------------------------------------------ subclass API

    @abstractmethod
    async def _inspect(self) -> Inspection: ...

    async def _do_provision(self) -> None:
        raise self._unsupported(ComputeOperation.PROVISION)

    async def _do_start(self) -> None:
        raise self._unsupported(ComputeOperation.START)

    async def _do_stop(self) -> None:
        raise self._unsupported(ComputeOperation.STOP)

    async def _do_deprovision(self) -> None:
        raise self._unsupported(ComputeOperation.DEPROVISION)

    # ------------------------------------------------------------------ port

    def supported_operations(self) -> list[ComputeOperation]:
        ops = [
            ComputeOperation.DESCRIBE,
            ComputeOperation.GET_RESOURCES,
            ComputeOperation.GET_STATUS,
        ]
        if self.config.allow_lifecycle:
            ops += [op for op in LIFECYCLE_OPERATIONS if op in self.implemented]
        return ops

    async def get_status(self) -> ComputeState:
        return (await self._inspect()).state

    async def get_resources(self) -> ComputeResources | None:
        return (await self._inspect()).resources

    async def describe_environment(self) -> ComputeEnvironment:
        inspection = await self._inspect()
        endpoints = []
        if inspection.endpoint_host and self.config.endpoint_port:
            endpoints.append(await self._probe(inspection))
        notes = list(inspection.notes)
        if not inspection.exists:
            notes.append("Not provisioned.")
        if not self.config.allow_lifecycle and any(
            op in self.implemented for op in LIFECYCLE_OPERATIONS
        ):
            notes.append("Lifecycle operations are disabled (set allow_lifecycle: true to enable).")
        return ComputeEnvironment(
            id=self.environment_id,
            kind=self.kind,
            display_name=self.config.display_name or self.environment_id,
            ephemeral=self.ephemeral,
            managed="automatic",
            state=inspection.state,
            resources=inspection.resources,
            operations=self.supported_operations(),
            endpoints=endpoints,
            notes=notes,
            details=inspection.details,
            checked_at=utcnow(),
        )

    async def provision(self) -> ComputeEnvironment:
        return await self._operate(ComputeOperation.PROVISION, self._do_provision)

    async def start(self) -> ComputeEnvironment:
        return await self._operate(ComputeOperation.START, self._do_start)

    async def stop(self) -> ComputeEnvironment:
        return await self._operate(ComputeOperation.STOP, self._do_stop)

    async def deprovision(self) -> ComputeEnvironment:
        return await self._operate(ComputeOperation.DEPROVISION, self._do_deprovision)

    async def wait_for(
        self, states: set[ComputeState], *, timeout_s: float | None = None, interval_s: float = 2.0
    ) -> ComputeState:
        """Poll until the environment reaches one of ``states`` (or the timeout elapses)."""
        deadline = time.monotonic() + (timeout_s or self.config.operation_timeout_s)
        while True:
            state = await self.get_status()
            if state in states or time.monotonic() >= deadline:
                return state
            await asyncio.sleep(interval_s)

    # ------------------------------------------------------------------ helpers

    async def _operate(self, op: ComputeOperation, action: Any) -> ComputeEnvironment:
        if op not in self.implemented:
            raise self._unsupported(op)
        if not self.config.allow_lifecycle:
            raise LifecycleDisabled(
                "lifecycle operations are disabled for compute environment "
                f"{self.environment_id!r}",
                details={
                    "environment_id": self.environment_id,
                    "operation": op.value,
                    "hint": "set allow_lifecycle: true in the environment's configuration",
                },
            )
        async with self._lock:
            await action()
        return await self.describe_environment()

    async def _probe(self, inspection: Inspection) -> EndpointInfo:
        url = f"{self.config.endpoint_scheme}://{inspection.endpoint_host}:{self.config.endpoint_port}"
        info = EndpointInfo(url=url, authorized=True)
        if not self.config.probe_endpoint or inspection.state != ComputeState.ONLINE:
            return info
        try:
            await self._egress.check_url(url)
            async with httpx.AsyncClient(
                timeout=5.0, follow_redirects=False, transport=self._probe_transport
            ) as client:
                started = time.perf_counter()
                response = await client.get(url.rstrip("/") + self.config.health_path)
            info.reachable = response.status_code < 500
            info.message = (
                f"HTTP {response.status_code} in {(time.perf_counter() - started) * 1000:.0f} ms"
            )
        except OMFError as exc:
            info.reachable = False
            info.message = exc.message
        except httpx.HTTPError as exc:
            info.reachable = False
            info.message = f"unreachable ({type(exc).__name__})"
        info.checked_at = utcnow()
        return info


def owned(labels: dict[str, str] | None, environment_id: str) -> bool:
    labels = labels or {}
    return labels.get(OWNER_LABEL) == "true" and labels.get(ENVIRONMENT_LABEL) == environment_id


def pinned_image(image: str) -> str:
    """Validate that a container image is pinned to a version tag or digest."""
    last = image.rsplit("/", 1)[-1]
    if "@sha256:" not in image and ":" not in last:
        raise ValueError("image must be pinned to a tag or digest (e.g. ollama/ollama:0.40.6)")
    if last.endswith(":latest"):
        raise ValueError("':latest' is not reproducible; pin a version tag or digest")
    return image
