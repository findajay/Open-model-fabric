"""Google Compute Engine adapter: a (GPU) VM running an inference server.

Uses the Compute Engine REST API (v1) with httpx; tokens come from a service-account JSON
(secret reference) via google-auth.

* ``provision`` inserts the instance, labelled ``omf-managed=true`` and
  ``omf-environment=<id>``, with optional GPUs and a startup script.
* ``start`` and ``stop`` start and stop it.
* ``deprovision`` deletes it, but only if it carries the OMF labels.

An instance that already exists without the labels is never modified. Mutating calls
return long-running operations. The adapter polls them for up to ``operation_wait_s``
and reports errors from operations that finish within that window. Use
``spot: true`` for preemptible capacity; the environment is then reported as
ephemeral.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from pydantic import Field

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
from omf.core.domain.models import ComputeResources, ComputeState, GpuInfo
from omf.core.ports.secrets import SecretRefStr
from omf.core.security.egress import EgressPolicy

ADAPTER_TYPE = "gcp_compute"
SCOPE = "https://www.googleapis.com/auth/compute"
_STATE = {
    "RUNNING": ComputeState.ONLINE,
    "PROVISIONING": ComputeState.DEGRADED,
    "STAGING": ComputeState.DEGRADED,
    "STOPPING": ComputeState.DEGRADED,
    "SUSPENDING": ComputeState.DEGRADED,
    "REPAIRING": ComputeState.DEGRADED,
    "TERMINATED": ComputeState.OFFLINE,
    "STOPPED": ComputeState.OFFLINE,
    "SUSPENDED": ComputeState.OFFLINE,
}
_NAME = r"^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$"


class GcpComputeConfig(ManagedComputeConfig):
    project: str = Field(pattern=r"^[a-z][-a-z0-9]{4,28}[a-z0-9]$")
    zone: str = Field(pattern=r"^[a-z]+-[a-z]+[0-9]+-[a-z]$", examples=["us-central1-a"])
    instance_name: str = Field(pattern=_NAME)
    credentials_ref: SecretRefStr | None = None
    machine_type: str = "g2-standard-4"
    source_image: str = "projects/cos-cloud/global/images/family/cos-stable"
    disk_size_gb: int = Field(default=100, ge=10, le=65_536)
    accelerator_type: str | None = Field(default=None, examples=["nvidia-l4", "nvidia-tesla-t4"])
    accelerator_count: int = Field(default=0, ge=0, le=16)
    network: str = "global/networks/default"
    subnetwork: str | None = None
    external_ip: bool = False
    startup_script: str | None = Field(default=None, max_length=256_000)
    service_account_email: str | None = None
    spot: bool = False
    api_base: str = "https://compute.googleapis.com/compute/v1"
    operation_wait_s: float = Field(default=30.0, ge=0, le=600)


class GcpComputeAdapter(ManagedComputeAdapter):
    kind = ADAPTER_TYPE

    def __init__(
        self,
        environment_id: str,
        config: GcpComputeConfig,
        egress: EgressPolicy,
        token: TokenSource,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(environment_id, config, egress)
        self.cfg = config
        self.ephemeral = config.spot
        self.api = RestClient(
            name="Compute Engine API", base_url=config.api_base, token=token, transport=transport
        )
        self._zone_path = f"/projects/{config.project}/zones/{config.zone}"
        self._instance_path = f"{self._zone_path}/instances/{config.instance_name}"
        self._machine_types: dict[str, dict[str, Any]] = {}

    async def aclose(self) -> None:
        await self.api.aclose()

    # ------------------------------------------------------------------ helpers

    async def _wait_operation(self, operation: dict[str, Any]) -> None:
        deadline = time.monotonic() + self.cfg.operation_wait_s
        while operation.get("status") != "DONE" and time.monotonic() < deadline:
            await asyncio.sleep(2.0)
            operation = await self.api.request(
                "GET", f"{self._zone_path}/operations/{operation['name']}"
            )
        errors = (operation.get("error") or {}).get("errors") or []
        if errors:
            raise ComputeError(
                "Compute Engine operation failed: "
                + "; ".join(str(e.get("message", e))[:200] for e in errors),
                details={"operation": operation.get("name")},
            )

    async def _machine_type(self, name: str) -> dict[str, Any]:
        if name not in self._machine_types:
            self._machine_types[name] = await self.api.request(
                "GET", f"{self._zone_path}/machineTypes/{name}"
            )
        return dict(self._machine_types[name])

    async def _resources(
        self, machine_type: str, accelerators: list[dict[str, Any]]
    ) -> ComputeResources:
        mt = await self._machine_type(machine_type)
        gpus = [
            GpuInfo(name=str(a.get("acceleratorType", "")).rsplit("/", 1)[-1])
            for a in accelerators
            for _ in range(int(a.get("acceleratorCount", 0)))
        ]
        memory_mb = mt.get("memoryMb")
        return ComputeResources(
            cpu_count=mt.get("guestCpus"),
            memory_total_bytes=int(memory_mb) * 2**20 if memory_mb else None,
            gpus=gpus,
            accelerator="cuda" if any(g.name.startswith("nvidia") for g in gpus) else "none",
            source="detected",
        )

    # ------------------------------------------------------------------ inspection

    async def _inspect(self) -> Inspection:
        instance = await self.api.request("GET", self._instance_path, allow_404=True)
        details: dict[str, Any] = {
            "project": self.cfg.project,
            "zone": self.cfg.zone,
            "instance": self.cfg.instance_name,
        }
        if instance is None:
            accelerators = (
                [
                    {
                        "acceleratorType": self.cfg.accelerator_type,
                        "acceleratorCount": self.cfg.accelerator_count,
                    }
                ]
                if self.cfg.accelerator_type
                else []
            )
            return Inspection(
                state=ComputeState.OFFLINE,
                exists=False,
                details=details,
                resources=await self._resources(self.cfg.machine_type, accelerators),
            )
        notes = []
        if not owned(instance.get("labels"), self.environment_id):
            notes.append(
                "An instance with this name exists but is not managed by Open Model "
                "Fabric; lifecycle operations will refuse to touch it."
            )
        machine_type = str(instance.get("machineType", "")).rsplit("/", 1)[-1]
        nic = (instance.get("networkInterfaces") or [{}])[0]
        public = ((nic.get("accessConfigs") or [{}])[0]).get("natIP")
        host = public if self.cfg.external_ip else nic.get("networkIP")
        status = instance.get("status", "")
        details.update(
            status=status,
            machine_type=machine_type,
            provisioning_model=(instance.get("scheduling") or {}).get("provisioningModel"),
        )
        return Inspection(
            state=_STATE.get(status, ComputeState.UNKNOWN),
            exists=True,
            resources=await self._resources(machine_type, instance.get("guestAccelerators") or []),
            endpoint_host=host,
            notes=notes,
            details=details,
        )

    # ------------------------------------------------------------------ lifecycle

    def _insert_body(self) -> dict[str, Any]:
        c = self.cfg
        nic: dict[str, Any] = {"network": c.network}
        if c.subnetwork:
            nic["subnetwork"] = c.subnetwork
        if c.external_ip:
            nic["accessConfigs"] = [{"type": "ONE_TO_ONE_NAT", "name": "External NAT"}]
        body: dict[str, Any] = {
            "name": c.instance_name,
            "machineType": f"zones/{c.zone}/machineTypes/{c.machine_type}",
            # Environment ids are lowercase slugs, which are valid GCE label values.
            "labels": {OWNER_LABEL: "true", ENVIRONMENT_LABEL: self.environment_id},
            "disks": [
                {
                    "boot": True,
                    "autoDelete": True,
                    "initializeParams": {
                        "sourceImage": c.source_image,
                        "diskSizeGb": str(c.disk_size_gb),
                    },
                }
            ],
            "networkInterfaces": [nic],
            # GPUs cannot live-migrate: Compute Engine requires TERMINATE on host maintenance.
            "scheduling": {
                "onHostMaintenance": "TERMINATE" if c.accelerator_type or c.spot else "MIGRATE",
                "automaticRestart": not c.spot,
                **(
                    {"provisioningModel": "SPOT", "instanceTerminationAction": "STOP"}
                    if c.spot
                    else {}
                ),
            },
        }
        if c.accelerator_type and c.accelerator_count:
            body["guestAccelerators"] = [
                {
                    "acceleratorType": f"zones/{c.zone}/acceleratorTypes/{c.accelerator_type}",
                    "acceleratorCount": c.accelerator_count,
                }
            ]
        if c.startup_script:
            body["metadata"] = {"items": [{"key": "startup-script", "value": c.startup_script}]}
        if c.service_account_email:
            body["serviceAccounts"] = [
                {
                    "email": c.service_account_email,
                    "scopes": ["https://www.googleapis.com/auth/cloud-platform"],
                }
            ]
        return body

    async def _owned_instance(self) -> dict[str, Any]:
        instance = await self.api.request("GET", self._instance_path, allow_404=True)
        if instance is None:
            raise ComputeError(
                f"instance {self.cfg.instance_name} is not provisioned",
                details={"hint": "run provision first"},
            )
        if not owned(instance.get("labels"), self.environment_id):
            raise Conflict(
                f"instance {self.cfg.instance_name} is not managed by Open Model Fabric "
                f"environment {self.environment_id!r}; refusing to touch it"
            )
        return dict(instance)

    async def _do_provision(self) -> None:
        if await self.api.request("GET", self._instance_path, allow_404=True) is not None:
            await self._owned_instance()  # conflict check; otherwise idempotent
            return
        await self._wait_operation(
            await self.api.request("POST", f"{self._zone_path}/instances", json=self._insert_body())
        )

    async def _do_start(self) -> None:
        await self._owned_instance()
        await self._wait_operation(await self.api.request("POST", f"{self._instance_path}/start"))

    async def _do_stop(self) -> None:
        await self._owned_instance()
        await self._wait_operation(await self.api.request("POST", f"{self._instance_path}/stop"))

    async def _do_deprovision(self) -> None:
        if await self.api.request("GET", self._instance_path, allow_404=True) is None:
            return
        await self._owned_instance()
        await self._wait_operation(await self.api.request("DELETE", self._instance_path))
