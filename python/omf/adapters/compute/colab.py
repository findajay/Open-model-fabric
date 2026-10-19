"""Google Colab notebook environment — manually managed and ephemeral.

What this adapter does:

* Records operator-supplied metadata about a notebook runtime (notebook URL, declared
  accelerator/RAM, notes).
* Optionally records an inference endpoint the operator registered by hand, *together
  with an explicit attestation* (``endpoint.authorized: true``) that exposing that
  endpoint is permitted by their Colab plan and Google's terms.
* Probes that endpoint's availability **only** when it is marked authorized.
* Reports explicit ``offline``/``unknown`` states otherwise.

What it deliberately does not do: provision, start, stop, reconnect or keep a notebook
alive. Colab is an interactive notebook product; it offers no supported API for
unattended runtime control, and free runtimes are not a web-service backend. All lifecycle
operations therefore raise ``OperationNotSupported``.
"""

from __future__ import annotations

import time
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from omf.core.domain.errors import OMFError
from omf.core.domain.models import (
    ComputeEnvironment,
    ComputeOperation,
    ComputeResources,
    ComputeState,
    EndpointInfo,
    GpuInfo,
    utcnow,
)
from omf.core.ports.compute import ComputeProvider
from omf.core.security.egress import EgressPolicy

ADAPTER_TYPE = "colab_notebook"


class ColabEndpointConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    authorized: bool = False
    """Operator attestation that this endpoint is permitted by the applicable Colab plan/terms.
    Unauthorized endpoints are recorded but never contacted."""
    health_path: str = "/"


class ColabNotebookConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = "Google Colab notebook"
    notebook_url: str | None = None
    declared_accelerator: Literal["none", "cuda", "tpu", "unknown"] = "unknown"
    declared_gpu: str | None = Field(default=None, examples=["T4", "L4", "A100"])
    declared_gpu_memory_gb: float | None = Field(default=None, gt=0)
    declared_ram_gb: float | None = Field(default=None, gt=0)
    endpoint: ColabEndpointConfig | None = None
    probe_timeout_s: float = Field(default=5.0, gt=0, le=30)


class ColabNotebookComputeAdapter(ComputeProvider):
    def __init__(
        self,
        environment_id: str,
        config: ColabNotebookConfig,
        egress: EgressPolicy,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(environment_id)
        self._config = config
        self._egress = egress
        self._transport = transport

    def supported_operations(self) -> list[ComputeOperation]:
        return [
            ComputeOperation.DESCRIBE,
            ComputeOperation.GET_RESOURCES,
            ComputeOperation.GET_STATUS,
        ]

    async def get_resources(self) -> ComputeResources:
        c = self._config
        gpus = []
        if c.declared_gpu:
            gpus.append(
                GpuInfo(
                    name=c.declared_gpu,
                    memory_total_bytes=int(c.declared_gpu_memory_gb * 1024**3)
                    if c.declared_gpu_memory_gb
                    else None,
                )
            )
        return ComputeResources(
            memory_total_bytes=int(c.declared_ram_gb * 1024**3) if c.declared_ram_gb else None,
            gpus=gpus,
            accelerator=c.declared_accelerator,
            source="declared",
        )

    async def _probe(self) -> EndpointInfo | None:
        ep = self._config.endpoint
        if ep is None:
            return None
        info = EndpointInfo(url=ep.url, authorized=ep.authorized)
        if not ep.authorized:
            info.message = (
                "endpoint not marked authorized; it is recorded but not contacted. "
                "Set endpoint.authorized only if your Colab plan and terms permit it."
            )
            return info
        try:
            await self._egress.check_url(ep.url)
            async with httpx.AsyncClient(
                timeout=self._config.probe_timeout_s,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                started = time.perf_counter()
                response = await client.get(ep.url.rstrip("/") + ep.health_path)
                info.reachable = response.status_code < 500
                info.message = (
                    f"HTTP {response.status_code} in "
                    f"{(time.perf_counter() - started) * 1000:.0f} ms"
                )
        except OMFError as exc:
            info.reachable = False
            info.message = exc.message
        except httpx.HTTPError as exc:
            info.reachable = False
            info.message = (
                f"unreachable ({type(exc).__name__}); the notebook runtime may have stopped"
            )
        info.checked_at = utcnow()
        return info

    async def get_status(self) -> ComputeState:
        return _state(await self._probe())

    async def describe_environment(self) -> ComputeEnvironment:
        endpoint = await self._probe()
        notes = [
            "Manually managed, ephemeral notebook runtime: Open Model Fabric cannot start, stop, "
            "reconnect or keep it alive.",
            "Resources are declared by the operator, not detected.",
        ]
        if endpoint is None:
            notes.append(
                "No endpoint registered: use the notebook directly for experiments "
                "(notebooks/colab_model_lab.ipynb)."
            )
        return ComputeEnvironment(
            id=self.environment_id,
            kind=ADAPTER_TYPE,
            display_name=self._config.display_name,
            ephemeral=True,
            managed="manual",
            state=_state(endpoint),
            resources=await self.get_resources(),
            operations=self.supported_operations(),
            endpoints=[endpoint] if endpoint else [],
            notes=notes,
            details={"notebook_url": self._config.notebook_url}
            if self._config.notebook_url
            else {},
            checked_at=utcnow(),
        )


def _state(endpoint: EndpointInfo | None) -> ComputeState:
    if endpoint is None:
        return ComputeState.OFFLINE  # nothing registered: explicitly offline for the platform
    if not endpoint.authorized:
        return ComputeState.UNKNOWN
    return ComputeState.ONLINE if endpoint.reachable else ComputeState.OFFLINE
