"""Google Cloud Run adapter: an OMF-managed Cloud Run (v2) service running an inference
container, optionally with one NVIDIA GPU.

Uses the Cloud Run Admin API v2 (``run.googleapis.com/v2``) with httpx; tokens come from
a service-account JSON (secret reference) via google-auth.

* ``provision`` creates the service, labelled ``omf-managed=true`` /
  ``omf-environment=<id>``, with service-level ``scaling.minInstanceCount: 0``.
* ``start`` sets ``scaling.minInstanceCount`` to ``min_instances`` (>= 1), keeping
  instances warm.
* ``stop`` sets it back to 0. **Stop means scale to zero, not stopped**: the service
  still exists and still starts an instance for the next request (a cold start).
  Deprovision to remove it.
* ``deprovision`` deletes the service, but only if it carries the OMF labels.

Scaling changes patch the service-level ``scaling`` field (``updateMask``), so they do
not create a new revision. Mutations return long-running operations, which are polled
for up to ``operation_wait_s``.

GPU: ``gpu_type`` (e.g. ``nvidia-l4``) sets ``template.nodeSelector.accelerator`` and the
``nvidia.com/gpu: "1"`` limit, the only value Cloud Run accepts. It also keeps CPU
allocated (``cpuIdle: false``). These field names were checked against the Cloud Run v2
discovery document. Check GPU quota and region availability before enabling a GPU.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, field_validator, model_validator

from omf.adapters.compute._managed import (
    ENVIRONMENT_LABEL,
    OWNER_LABEL,
    Inspection,
    ManagedComputeAdapter,
    ManagedComputeConfig,
    owned,
    pinned_image,
)
from omf.adapters.compute._rest import RestClient, TokenSource
from omf.core.domain.errors import ComputeError, Conflict
from omf.core.domain.models import ComputeResources, ComputeState, GpuInfo
from omf.core.ports.secrets import SecretRefStr
from omf.core.security.egress import EgressPolicy

ADAPTER_TYPE = "gcp_cloud_run"
SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class GcpCloudRunComputeConfig(ManagedComputeConfig):
    project: str = Field(pattern=r"^[a-z][-a-z0-9]{4,28}[a-z0-9]$")
    region: str = Field(pattern=r"^[a-z]+-[a-z]+[0-9]+$", examples=["us-central1"])
    service: str = Field(pattern=r"^[a-z]([-a-z0-9]{0,47}[a-z0-9])?$")
    credentials_ref: SecretRefStr | None = None
    image: str
    command: list[str] = Field(default_factory=list)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    env_secret_manager_refs: dict[str, str] = Field(
        default_factory=dict,
        description="Env var name -> Secret Manager 'secret' or 'secret:version' "
        "(resolved by Cloud Run; values never pass through OMF)",
    )
    container_port: int = Field(default=8000, ge=1, le=65535)
    cpu: Literal["1", "2", "4", "8"] = "4"
    memory: str = "16Gi"
    gpu_type: str | None = Field(default=None, examples=["nvidia-l4"])
    min_instances: int = Field(default=1, ge=1, le=100)
    """Instances kept warm while started."""
    max_instances: int = Field(default=1, ge=1, le=1000)
    ingress: Literal[
        "INGRESS_TRAFFIC_ALL",
        "INGRESS_TRAFFIC_INTERNAL_ONLY",
        "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER",
    ] = "INGRESS_TRAFFIC_ALL"
    service_account_email: str | None = None
    api_base: str = "https://run.googleapis.com/v2"
    operation_wait_s: float = Field(default=60.0, ge=0, le=900)
    endpoint_scheme: str = Field(default="https", pattern=r"^https?$")

    @field_validator("image")
    @classmethod
    def _pinned(cls, v: str) -> str:
        return pinned_image(v)

    @model_validator(mode="after")
    def _check(self) -> GcpCloudRunComputeConfig:
        if self.min_instances > self.max_instances:
            raise ValueError("min_instances must not exceed max_instances")
        if self.endpoint_port is None:
            self.endpoint_port = 443 if self.endpoint_scheme == "https" else 80
        return self


class GcpCloudRunComputeAdapter(ManagedComputeAdapter):
    kind = ADAPTER_TYPE

    def __init__(
        self,
        environment_id: str,
        config: GcpCloudRunComputeConfig,
        egress: EgressPolicy,
        token: TokenSource,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        probe_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(environment_id, config, egress, probe_transport=probe_transport)
        self.cfg = config
        self.api = RestClient(
            name="Cloud Run API", base_url=config.api_base, token=token, transport=transport
        )
        self._parent = f"/projects/{config.project}/locations/{config.region}"
        self._service_path = f"{self._parent}/services/{config.service}"

    async def aclose(self) -> None:
        await self.api.aclose()

    # ------------------------------------------------------------------ helpers

    async def _wait_operation(self, operation: dict[str, Any]) -> None:
        deadline = time.monotonic() + self.cfg.operation_wait_s
        while not operation.get("done") and time.monotonic() < deadline:
            await asyncio.sleep(2.0)
            operation = await self.api.request("GET", f"/{operation['name']}")
        error = operation.get("error")
        if error:
            raise ComputeError(
                f"Cloud Run operation failed: {str(error.get('message', error))[:300]}",
                details={"operation": operation.get("name"), "code": error.get("code")},
            )

    async def _get(self) -> dict[str, Any] | None:
        service = await self.api.request("GET", self._service_path, allow_404=True)
        return dict(service) if service is not None else None

    def _check_owned(self, service: dict[str, Any]) -> None:
        if not owned(service.get("labels"), self.environment_id):
            raise Conflict(
                f"Cloud Run service {self.cfg.service!r} is not managed by Open Model Fabric "
                f"environment {self.environment_id!r}; refusing to touch it"
            )

    def _resources(self) -> ComputeResources:
        c = self.cfg
        gpus = [GpuInfo(name=c.gpu_type)] if c.gpu_type else []
        memory = c.memory.strip()
        size = {"Mi": 2**20, "Gi": 2**30}.get(memory[-2:])
        return ComputeResources(
            cpu_count=int(c.cpu),
            memory_total_bytes=int(float(memory[:-2]) * size) if size else None,
            gpus=gpus,
            accelerator="cuda" if gpus else "none",
            source="declared",
        )

    # ------------------------------------------------------------------ inspection

    async def _inspect(self) -> Inspection:
        service = await self._get()
        details: dict[str, Any] = {
            "project": self.cfg.project,
            "region": self.cfg.region,
            "service": self.cfg.service,
        }
        notes = ["Resources are the service's configured limits, not detected hardware."]
        if service is None:
            return Inspection(
                state=ComputeState.OFFLINE,
                exists=False,
                resources=self._resources(),
                notes=notes,
                details=details,
            )
        if not owned(service.get("labels"), self.environment_id):
            notes.append(
                "A service with this name exists but is not managed by Open Model Fabric; "
                "lifecycle operations will refuse to touch it."
            )
        condition = service.get("terminalCondition") or {}
        warm = int((service.get("scaling") or {}).get("minInstanceCount") or 0)
        if condition.get("state") == "CONDITION_FAILED":
            state = ComputeState.DEGRADED
            notes.append(f"Cloud Run reports: {str(condition.get('message', ''))[:200]}")
        elif service.get("reconciling"):
            state = ComputeState.DEGRADED
        elif warm >= 1:
            state = ComputeState.ONLINE
        else:
            state = ComputeState.OFFLINE
            notes.append(
                "Scaled to zero: the service still exists and starts an instance on the next "
                "request (cold start)."
            )
        details.update(
            min_instances=warm,
            latest_ready_revision=service.get("latestReadyRevision"),
            uri=service.get("uri"),
        )
        host = urlsplit(service["uri"]).hostname if service.get("uri") else None
        return Inspection(
            state=state,
            exists=True,
            resources=self._resources(),
            endpoint_host=host,
            notes=notes,
            details=details,
        )

    # ------------------------------------------------------------------ lifecycle

    def _body(self) -> dict[str, Any]:
        c = self.cfg
        env: list[dict[str, Any]] = [{"name": k, "value": v} for k, v in c.env.items()]
        for name, ref in c.env_secret_manager_refs.items():
            secret, _, version = ref.partition(":")
            env.append(
                {
                    "name": name,
                    "valueSource": {
                        "secretKeyRef": {"secret": secret, "version": version or "latest"}
                    },
                }
            )
        limits = {"cpu": c.cpu, "memory": c.memory}
        if c.gpu_type:
            limits["nvidia.com/gpu"] = "1"
        container: dict[str, Any] = {
            "image": c.image,
            "ports": [{"containerPort": c.container_port}],
            "env": env,
            # resources set => cpuIdle must be explicit; GPUs need CPU always allocated.
            "resources": {"limits": limits, "cpuIdle": not c.gpu_type},
        }
        if c.command:
            container["command"] = c.command
        if c.args:
            container["args"] = c.args
        template: dict[str, Any] = {
            "containers": [container],
            "scaling": {"minInstanceCount": 0, "maxInstanceCount": c.max_instances},
            "labels": {OWNER_LABEL: "true", ENVIRONMENT_LABEL: self.environment_id},
        }
        if c.gpu_type:
            template["nodeSelector"] = {"accelerator": c.gpu_type}
        if c.service_account_email:
            template["serviceAccount"] = c.service_account_email
        return {
            "labels": {OWNER_LABEL: "true", ENVIRONMENT_LABEL: self.environment_id},
            "ingress": c.ingress,
            "scaling": {"minInstanceCount": 0},
            "template": template,
        }

    async def _do_provision(self) -> None:
        existing = await self._get()
        if existing is not None:
            self._check_owned(existing)
            return  # idempotent
        await self._wait_operation(
            await self.api.request(
                "POST",
                f"{self._parent}/services",
                params={"serviceId": self.cfg.service},
                json=self._body(),
            )
        )

    async def _scale(self, warm: int) -> None:
        service = await self._get()
        if service is None:
            raise ComputeError(
                f"Cloud Run service {self.cfg.service!r} is not provisioned",
                details={"hint": "run provision first"},
            )
        self._check_owned(service)
        await self._wait_operation(
            await self.api.request(
                "PATCH",
                self._service_path,
                params={"updateMask": "scaling.minInstanceCount"},
                json={"scaling": {"minInstanceCount": warm}},
            )
        )

    async def _do_start(self) -> None:
        await self._scale(self.cfg.min_instances)

    async def _do_stop(self) -> None:
        await self._scale(0)

    async def _do_deprovision(self) -> None:
        service = await self._get()
        if service is None:
            return
        self._check_owned(service)
        await self._wait_operation(await self.api.request("DELETE", self._service_path))
