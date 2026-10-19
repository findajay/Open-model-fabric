"""Azure virtual machine adapter: start/stop an existing (GPU) VM that runs an inference server.

Uses the Azure Resource Manager REST API with httpx and a service principal (client
credentials; the secret is referenced, never inlined).

Scope: the VM is created outside Open Model Fabric (Bicep/Terraform/portal) because an
Azure VM needs a NIC, VNet, disk and often a public IP. ``provision`` and ``deprovision``
are therefore not supported. ``start`` powers the VM on; ``stop`` **deallocates** it.
A VM that is merely powered off still bills compute, so stop always deallocates.
Mutating calls are asynchronous in Azure; the adapter returns once Azure accepted them.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from pydantic import Field

from omf.adapters.compute._managed import Inspection, ManagedComputeAdapter, ManagedComputeConfig
from omf.adapters.compute._rest import RestClient, TokenSource
from omf.core.domain.errors import ComputeError
from omf.core.domain.models import ComputeOperation, ComputeResources, ComputeState
from omf.core.ports.secrets import SecretRefStr, SecretValue
from omf.core.security.egress import EgressPolicy

ADAPTER_TYPE = "azure_vm"
API_VERSION = "2024-07-01"
_POWER = {
    "PowerState/running": ComputeState.ONLINE,
    "PowerState/starting": ComputeState.DEGRADED,
    "PowerState/stopping": ComputeState.DEGRADED,
    "PowerState/deallocating": ComputeState.DEGRADED,
    "PowerState/stopped": ComputeState.OFFLINE,
    "PowerState/deallocated": ComputeState.OFFLINE,
}


class AzureVmComputeConfig(ManagedComputeConfig):
    subscription_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    resource_group: str
    vm_name: str
    tenant_id: str
    client_id: str
    client_secret_ref: SecretRefStr
    endpoint_host: str | None = None
    """DNS name or IP of the inference server on the VM (Azure does not expose it on the VM)."""
    management_url: str = "https://management.azure.com"
    authority_url: str = "https://login.microsoftonline.com"


class AzureClientCredentials:
    """OAuth2 client-credentials tokens for Azure Resource Manager, cached until expiry."""

    def __init__(
        self,
        config: AzureVmComputeConfig,
        secret: SecretValue,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._secret = secret
        self._transport = transport
        self._token: str | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def __call__(self) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._expires - 60:
                return self._token
            c = self._config
            async with httpx.AsyncClient(
                timeout=30, transport=self._transport, follow_redirects=False
            ) as client:
                response = await client.post(
                    f"{c.authority_url}/{c.tenant_id}/oauth2/v2.0/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": c.client_id,
                        "client_secret": self._secret.reveal(),
                        "scope": f"{c.management_url}/.default",
                    },
                )
            if response.status_code != 200:
                raise ComputeError(
                    f"Azure authentication failed (HTTP {response.status_code})",
                    details={"status": response.status_code},
                )
            body = response.json()
            self._token = str(body["access_token"])
            self._expires = time.monotonic() + float(body.get("expires_in", 3600))
            return self._token


class AzureVmComputeAdapter(ManagedComputeAdapter):
    kind = ADAPTER_TYPE
    implemented = (ComputeOperation.START, ComputeOperation.STOP)

    def __init__(
        self,
        environment_id: str,
        config: AzureVmComputeConfig,
        egress: EgressPolicy,
        token: TokenSource,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(environment_id, config, egress)
        self.cfg = config
        self.api = RestClient(
            name="Azure Resource Manager",
            base_url=config.management_url,
            token=token,
            transport=transport,
        )
        self._vm_path = (
            f"/subscriptions/{config.subscription_id}/resourceGroups/"
            f"{config.resource_group}/providers/Microsoft.Compute/virtualMachines/"
            f"{config.vm_name}"
        )
        self._sizes: dict[str, dict[str, Any]] = {}

    async def aclose(self) -> None:
        await self.api.aclose()

    async def _size(self, location: str, size: str) -> dict[str, Any]:
        if location not in self._sizes:
            body = await self.api.request(
                "GET",
                f"/subscriptions/{self.cfg.subscription_id}/providers/Microsoft.Compute/"
                f"locations/{location}/vmSizes",
                params={"api-version": API_VERSION},
            )
            self._sizes[location] = {s["name"]: s for s in body.get("value", [])}
        return dict(self._sizes[location].get(size, {}))

    async def _inspect(self) -> Inspection:
        vm = await self.api.request(
            "GET",
            self._vm_path,
            allow_404=True,
            params={"api-version": API_VERSION, "$expand": "instanceView"},
        )
        details: dict[str, Any] = {
            "resource_group": self.cfg.resource_group,
            "vm": self.cfg.vm_name,
        }
        if vm is None:
            return Inspection(
                state=ComputeState.OFFLINE,
                exists=False,
                details=details,
                notes=["VM not found; create it with Bicep/Terraform first."],
            )
        props = vm.get("properties", {})
        statuses = (props.get("instanceView") or {}).get("statuses") or []
        power = next(
            (s.get("code") for s in statuses if str(s.get("code", "")).startswith("PowerState/")),
            None,
        )
        size = (props.get("hardwareProfile") or {}).get("vmSize", "")
        info = await self._size(vm.get("location", ""), size) if size else {}
        resources = ComputeResources(
            cpu_count=info.get("numberOfCores"),
            memory_total_bytes=int(info["memoryInMB"]) * 2**20 if info.get("memoryInMB") else None,
            # The sizes API does not describe GPUs; N-series sizes carry NVIDIA GPUs.
            accelerator="cuda" if size.startswith("Standard_N") else "unknown",
            source="detected",
        )
        details.update(vm_size=size, power_state=power, location=vm.get("location"))
        notes = []
        if size.startswith("Standard_N"):
            notes.append("GPU model is implied by the VM size (Azure does not report it).")
        return Inspection(
            state=_POWER.get(str(power), ComputeState.UNKNOWN),
            exists=True,
            resources=resources,
            endpoint_host=self.cfg.endpoint_host,
            notes=notes,
            details=details,
        )

    async def _require(self) -> None:
        if (
            await self.api.request(
                "GET", self._vm_path, allow_404=True, params={"api-version": API_VERSION}
            )
            is None
        ):
            raise ComputeError(f"VM {self.cfg.vm_name} not found in {self.cfg.resource_group}")

    async def _do_start(self) -> None:
        await self._require()
        await self.api.request(
            "POST", f"{self._vm_path}/start", params={"api-version": API_VERSION}
        )

    async def _do_stop(self) -> None:
        await self._require()
        await self.api.request(
            "POST", f"{self._vm_path}/deallocate", params={"api-version": API_VERSION}
        )
