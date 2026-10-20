"""Azure virtual machine adapter: a (GPU) VM that runs an inference server.

Uses the Azure Resource Manager REST API with httpx and a service principal (client
credentials; the secret is referenced, never inlined). API versions:
``Microsoft.Compute/virtualMachines`` ``2024-07-01`` and
``Microsoft.Network/networkInterfaces`` ``2024-07-01``.

Two modes, like ``aws_ec2``:

* **Managed VM** (provisioning settings present: ``location``, ``subnet_id``,
  ``vm_size``, ``image`` and ``ssh_public_key_ref``).
  * ``provision`` creates a NIC in the existing subnet, then the VM, with an SSH key,
    password login disabled and optional cloud-init ``custom_data``. Both are tagged
    ``omf-managed=true`` / ``omf-environment=<id>``.
  * The OS disk and the NIC are created with ``deleteOption: Delete``, so deleting the
    VM removes them too.
  * ``deprovision`` deletes the VM only if it carries the OMF tags.
* **Designated VM** (the VM exists without OMF tags, for example created with Bicep or
  Terraform). ``start`` and ``stop`` are allowed, ``provision`` is a no-op, and
  ``deprovision`` is refused.

The network (VNet, subnet, NSG) and any public IP or load balancer are created outside
OMF. The endpoint host is ``endpoint_host``, or the NIC's private IP for managed VMs.
``stop`` always **deallocates**, because a VM that is only powered off still bills
compute. Mutating calls are asynchronous in Azure; the adapter returns once Azure
accepted them. The only exception is NIC creation, which provisioning waits for because
the VM needs the NIC.
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from omf.adapters.compute._managed import (
    ENVIRONMENT_LABEL,
    OWNER_LABEL,
    Inspection,
    ManagedComputeAdapter,
    ManagedComputeConfig,
    owned,
)
from omf.adapters.compute._rest import RestClient, TokenSource
from omf.core.domain.errors import ComputeError, Conflict
from omf.core.domain.models import (
    LIFECYCLE_OPERATIONS,
    ComputeOperation,
    ComputeResources,
    ComputeState,
)
from omf.core.ports.secrets import SecretRefStr, SecretValue
from omf.core.security.egress import EgressPolicy

ADAPTER_TYPE = "azure_vm"
API_VERSION = "2024-07-01"
NETWORK_API_VERSION = "2024-07-01"
_POWER = {
    "PowerState/running": ComputeState.ONLINE,
    "PowerState/starting": ComputeState.DEGRADED,
    "PowerState/stopping": ComputeState.DEGRADED,
    "PowerState/deallocating": ComputeState.DEGRADED,
    "PowerState/stopped": ComputeState.OFFLINE,
    "PowerState/deallocated": ComputeState.OFFLINE,
}


class AzureArmConfig(ManagedComputeConfig):
    """Subscription, resource group and service-principal settings shared by Azure adapters."""

    subscription_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    resource_group: str
    tenant_id: str
    client_id: str
    client_secret_ref: SecretRefStr
    management_url: str = "https://management.azure.com"
    authority_url: str = "https://login.microsoftonline.com"


class AzureImage(BaseModel):
    """Marketplace image reference."""

    model_config = ConfigDict(extra="forbid")

    publisher: str
    offer: str
    sku: str
    version: str = "latest"


class AzureVmComputeConfig(AzureArmConfig):
    vm_name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}$")
    endpoint_host: str | None = None
    """DNS name or IP of the inference server; defaults to the NIC's private IP."""
    # Provisioning (all optional; without them the VM must already exist).
    location: str | None = Field(default=None, examples=["eastus"])
    subnet_id: str | None = Field(
        default=None,
        pattern=r"^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\.Network/"
        r"virtualNetworks/[^/]+/subnets/[^/]+$",
    )
    vm_size: str | None = Field(default=None, examples=["Standard_NC4as_T4_v3"])
    image: AzureImage | None = None
    admin_username: str = Field(default="azureuser", pattern=r"^[a-z_][a-z0-9_-]{0,31}$")
    ssh_public_key_ref: SecretRefStr | None = None
    custom_data: str | None = Field(default=None, max_length=64_000)
    """cloud-init user data that starts the inference server (base64-encoded by OMF)."""
    os_disk_gb: int | None = Field(default=None, ge=30, le=4095)

    @field_validator("image", mode="before")
    @classmethod
    def _urn(cls, v: Any) -> Any:
        # Accept the CLI's URN form "publisher:offer:sku:version".
        if isinstance(v, str):
            parts = v.split(":")
            if len(parts) != 4:
                raise ValueError("image URN must be publisher:offer:sku:version")
            return dict(zip(("publisher", "offer", "sku", "version"), parts, strict=True))
        return v

    @property
    def provisioning_configured(self) -> bool:
        return all(
            (self.location, self.subnet_id, self.vm_size, self.image, self.ssh_public_key_ref)
        )


class AzureClientCredentials:
    """OAuth2 client-credentials tokens for Azure Resource Manager, cached until expiry."""

    def __init__(
        self,
        config: AzureArmConfig,
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

    def __init__(
        self,
        environment_id: str,
        config: AzureVmComputeConfig,
        egress: EgressPolicy,
        token: TokenSource,
        *,
        ssh_public_key: SecretValue | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        nic_wait_s: float = 120.0,
    ) -> None:
        super().__init__(environment_id, config, egress)
        self.cfg = config
        self._ssh_key = ssh_public_key
        self._nic_wait_s = nic_wait_s
        # provision/deprovision only when the operator configured how to build the VM.
        self.implemented = (
            LIFECYCLE_OPERATIONS
            if config.provisioning_configured
            else (ComputeOperation.START, ComputeOperation.STOP)
        )
        self.api = RestClient(
            name="Azure Resource Manager",
            base_url=config.management_url,
            token=token,
            transport=transport,
        )
        group = f"/subscriptions/{config.subscription_id}/resourceGroups/{config.resource_group}"
        self._vm_path = f"{group}/providers/Microsoft.Compute/virtualMachines/{config.vm_name}"
        self._nic_path = (
            f"{group}/providers/Microsoft.Network/networkInterfaces/{config.vm_name}-nic"
        )
        self._sizes: dict[str, dict[str, Any]] = {}

    async def aclose(self) -> None:
        await self.api.aclose()

    # ------------------------------------------------------------------ helpers

    def _tags(self) -> dict[str, str]:
        return {OWNER_LABEL: "true", ENVIRONMENT_LABEL: self.environment_id}

    async def _vm(self, *, expand: bool = False) -> dict[str, Any] | None:
        params = {"api-version": API_VERSION}
        if expand:
            params["$expand"] = "instanceView"
        vm = await self.api.request("GET", self._vm_path, allow_404=True, params=params)
        return dict(vm) if vm is not None else None

    async def _nic(self) -> dict[str, Any] | None:
        nic = await self.api.request(
            "GET", self._nic_path, allow_404=True, params={"api-version": NETWORK_API_VERSION}
        )
        return dict(nic) if nic is not None else None

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

    @staticmethod
    def _private_ip(nic: dict[str, Any] | None) -> str | None:
        configs = ((nic or {}).get("properties") or {}).get("ipConfigurations") or [{}]
        return (configs[0].get("properties") or {}).get("privateIPAddress")

    # ------------------------------------------------------------------ inspection

    async def _inspect(self) -> Inspection:
        vm = await self._vm(expand=True)
        details: dict[str, Any] = {
            "resource_group": self.cfg.resource_group,
            "vm": self.cfg.vm_name,
        }
        if vm is None:
            notes = []
            if not self.cfg.provisioning_configured:
                notes.append(
                    "VM not found. Create it with Bicep/Terraform, or configure location, "
                    "subnet_id, vm_size, image and ssh_public_key_ref to let OMF provision it."
                )
            return Inspection(
                state=ComputeState.OFFLINE, exists=False, details=details, notes=notes
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
        is_owned = owned(vm.get("tags"), self.environment_id)
        details.update(
            vm_size=size,
            power_state=power,
            location=vm.get("location"),
            managed_by_omf=is_owned,
        )
        notes = []
        if size.startswith("Standard_N"):
            notes.append("GPU model is implied by the VM size (Azure does not report it).")
        if not is_owned:
            notes.append("Designated VM without OMF tags: start/stop allowed, deprovision refused.")
        host = self.cfg.endpoint_host
        if host is None and is_owned:
            host = self._private_ip(await self._nic())
        return Inspection(
            state=_POWER.get(str(power), ComputeState.UNKNOWN),
            exists=True,
            resources=resources,
            endpoint_host=host,
            notes=notes,
            details=details,
        )

    # ------------------------------------------------------------------ lifecycle

    async def _require(self) -> None:
        if await self._vm() is None:
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

    async def _ensure_nic(self) -> str:
        nic = await self._nic()
        if nic is not None and not owned(nic.get("tags"), self.environment_id):
            raise Conflict(
                f"network interface {self.cfg.vm_name}-nic exists but is not managed by Open "
                "Model Fabric; refusing to use it"
            )
        if nic is None:
            await self.api.request(
                "PUT",
                self._nic_path,
                params={"api-version": NETWORK_API_VERSION},
                json={
                    "location": self.cfg.location,
                    "tags": self._tags(),
                    "properties": {
                        "ipConfigurations": [
                            {
                                "name": "ipconfig1",
                                "properties": {
                                    "subnet": {"id": self.cfg.subnet_id},
                                    "privateIPAllocationMethod": "Dynamic",
                                },
                            }
                        ]
                    },
                },
            )
        # The VM can only reference a NIC that finished provisioning.
        deadline = time.monotonic() + self._nic_wait_s
        while True:
            nic = await self._nic()
            state = ((nic or {}).get("properties") or {}).get("provisioningState")
            if state == "Succeeded" and nic is not None:
                return str(nic["id"])
            if state == "Failed" or time.monotonic() >= deadline:
                raise ComputeError(
                    f"network interface {self.cfg.vm_name}-nic did not provision (state {state!r})"
                )
            await asyncio.sleep(2.0)

    def _vm_body(self, nic_id: str) -> dict[str, Any]:
        c = self.cfg
        assert c.image is not None
        assert self._ssh_key is not None
        os_profile: dict[str, Any] = {
            "computerName": c.vm_name,
            "adminUsername": c.admin_username,
            "linuxConfiguration": {
                "disablePasswordAuthentication": True,
                "ssh": {
                    "publicKeys": [
                        {
                            "path": f"/home/{c.admin_username}/.ssh/authorized_keys",
                            "keyData": self._ssh_key.reveal().strip(),
                        }
                    ]
                },
            },
        }
        if c.custom_data:
            os_profile["customData"] = base64.b64encode(c.custom_data.encode()).decode()
        os_disk: dict[str, Any] = {
            "createOption": "FromImage",
            "deleteOption": "Delete",
            "managedDisk": {"storageAccountType": "Premium_LRS"},
        }
        if c.os_disk_gb:
            os_disk["diskSizeGB"] = c.os_disk_gb
        return {
            "location": c.location,
            "tags": self._tags(),
            "properties": {
                "hardwareProfile": {"vmSize": c.vm_size},
                "storageProfile": {
                    "imageReference": c.image.model_dump(),
                    "osDisk": os_disk,
                },
                "osProfile": os_profile,
                "networkProfile": {
                    "networkInterfaces": [
                        {"id": nic_id, "properties": {"primary": True, "deleteOption": "Delete"}}
                    ]
                },
            },
        }

    async def _do_provision(self) -> None:
        if await self._vm() is not None:
            return  # idempotent; a designated VM is used as it is
        if self._ssh_key is None:
            raise ComputeError("ssh_public_key_ref could not be resolved")
        nic_id = await self._ensure_nic()
        await self.api.request(
            "PUT", self._vm_path, params={"api-version": API_VERSION}, json=self._vm_body(nic_id)
        )

    async def _do_deprovision(self) -> None:
        vm = await self._vm()
        if vm is None:
            # Clean up a NIC left behind by a provision that failed before the VM existed.
            nic = await self._nic()
            if nic is not None and owned(nic.get("tags"), self.environment_id):
                await self.api.request(
                    "DELETE", self._nic_path, params={"api-version": NETWORK_API_VERSION}
                )
            return
        if not owned(vm.get("tags"), self.environment_id):
            raise Conflict(
                f"VM {self.cfg.vm_name} was not created by Open Model Fabric; refusing to delete it"
            )
        # deleteOption=Delete on the OS disk and NIC removes them with the VM.
        await self.api.request("DELETE", self._vm_path, params={"api-version": API_VERSION})
