"""Azure Container Apps adapter: an OMF-managed container app running an inference server.

Uses the Azure Resource Manager REST API (``Microsoft.App/containerApps``, api-version
``2025-01-01``) with the same service-principal credentials as ``azure_vm``.

* ``provision`` creates (PUT) the app in an existing Container Apps environment
  (``managed_environment_id``). It is tagged ``omf-managed=true`` /
  ``omf-environment=<id>`` and created with ``minReplicas: 0``, so it scales to zero
  when idle.
* ``start`` and ``stop`` use Azure's own ``start`` and ``stop`` actions. A stopped app
  runs no replicas and does not serve traffic. Changing ``minReplicas`` alone would
  still cold-start on the next request, and rewriting the app would require resending
  its secret values.
* ``deprovision`` deletes the app, but only if it carries the OMF tags.

GPUs: set ``workload_profile_name`` to a GPU workload profile of the environment (for
example a serverless GPU profile). The environment, its network and its workload profiles
are created outside OMF. Secret environment values (``env_secret_refs``) are resolved by
OMF and stored as Container Apps secrets. Mutating calls are asynchronous in Azure; the
adapter returns once Azure accepted them.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import Field, field_validator, model_validator

from omf.adapters.compute._managed import (
    ENVIRONMENT_LABEL,
    OWNER_LABEL,
    Inspection,
    ManagedComputeAdapter,
    owned,
    pinned_image,
)
from omf.adapters.compute._rest import RestClient, TokenSource
from omf.adapters.compute.azure_vm import AzureArmConfig
from omf.core.domain.errors import ComputeError, Conflict
from omf.core.domain.models import ComputeResources, ComputeState
from omf.core.ports.secrets import SecretRefStr, SecretValue
from omf.core.security.egress import EgressPolicy

ADAPTER_TYPE = "azure_container_apps"
API_VERSION = "2025-01-01"
_RUNNING = {
    "Running": ComputeState.ONLINE,
    "Ready": ComputeState.ONLINE,
    "Progressing": ComputeState.DEGRADED,
    "Stopped": ComputeState.OFFLINE,
    "Suspended": ComputeState.OFFLINE,
}


class AzureContainerAppsComputeConfig(AzureArmConfig):
    app_name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,30}[a-z0-9]$")
    location: str = Field(examples=["swedencentral"])
    managed_environment_id: str = Field(
        pattern=r"^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\.App/"
        r"managedEnvironments/[^/]+$",
        description="ARM id of an existing Container Apps environment",
    )
    workload_profile_name: str | None = Field(
        default=None, description="e.g. a GPU workload profile defined on the environment"
    )
    image: str
    command: list[str] = Field(default_factory=list)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    env_secret_refs: dict[str, SecretRefStr] = Field(default_factory=dict)
    container_port: int = Field(default=8000, ge=1, le=65535)
    cpu: float = Field(default=2.0, gt=0, le=64)
    memory: str = "4Gi"
    min_replicas: int = Field(default=0, ge=0, le=300)
    max_replicas: int = Field(default=1, ge=1, le=300)
    external_ingress: bool = True
    endpoint_scheme: str = Field(default="https", pattern=r"^https?$")

    @field_validator("image")
    @classmethod
    def _pinned(cls, v: str) -> str:
        return pinned_image(v)

    @model_validator(mode="after")
    def _check(self) -> AzureContainerAppsComputeConfig:
        if self.min_replicas > self.max_replicas:
            raise ValueError("min_replicas must not exceed max_replicas")
        if self.endpoint_port is None:
            self.endpoint_port = 443 if self.endpoint_scheme == "https" else 80
        return self


def _secret_name(env_name: str) -> str:
    # Container Apps secret names: lowercase alphanumerics and '-'.
    return "omf-" + "".join(ch if ch.isalnum() else "-" for ch in env_name.lower())


class AzureContainerAppsComputeAdapter(ManagedComputeAdapter):
    kind = ADAPTER_TYPE

    def __init__(
        self,
        environment_id: str,
        config: AzureContainerAppsComputeConfig,
        egress: EgressPolicy,
        token: TokenSource,
        *,
        secrets: dict[str, SecretValue] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        probe_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(environment_id, config, egress, probe_transport=probe_transport)
        self.cfg = config
        self._secrets = secrets or {}
        self.api = RestClient(
            name="Azure Resource Manager",
            base_url=config.management_url,
            token=token,
            transport=transport,
        )
        self._path = (
            f"/subscriptions/{config.subscription_id}/resourceGroups/{config.resource_group}"
            f"/providers/Microsoft.App/containerApps/{config.app_name}"
        )
        self._params = {"api-version": API_VERSION}

    async def aclose(self) -> None:
        await self.api.aclose()

    async def _get(self) -> dict[str, Any] | None:
        app = await self.api.request("GET", self._path, allow_404=True, params=self._params)
        return dict(app) if app is not None else None

    def _check_owned(self, app: dict[str, Any]) -> None:
        if not owned(app.get("tags"), self.environment_id):
            raise Conflict(
                f"container app {self.cfg.app_name!r} is not managed by Open Model Fabric "
                f"environment {self.environment_id!r}; refusing to touch it"
            )

    def _resources(self) -> ComputeResources:
        memory = self.cfg.memory.strip()
        size = {"Mi": 2**20, "Gi": 2**30}.get(memory[-2:])
        return ComputeResources(
            cpu_count=max(1, int(self.cfg.cpu)),
            memory_total_bytes=int(float(memory[:-2]) * size) if size else None,
            # Container Apps does not report GPUs per app; they come from the profile.
            accelerator="unknown" if self.cfg.workload_profile_name else "none",
            source="declared",
        )

    # ------------------------------------------------------------------ inspection

    async def _inspect(self) -> Inspection:
        app = await self._get()
        details: dict[str, Any] = {
            "resource_group": self.cfg.resource_group,
            "app": self.cfg.app_name,
            "workload_profile": self.cfg.workload_profile_name,
        }
        notes = ["Resources are the app's configured CPU and memory, not detected hardware."]
        if self.cfg.workload_profile_name:
            notes.append("GPUs come from the workload profile; Azure does not report them per app.")
        if app is None:
            return Inspection(
                state=ComputeState.OFFLINE,
                exists=False,
                resources=self._resources(),
                notes=notes,
                details=details,
            )
        if not owned(app.get("tags"), self.environment_id):
            notes.append(
                "A container app with this name exists but is not managed by Open Model "
                "Fabric; lifecycle operations will refuse to touch it."
            )
        props = app.get("properties") or {}
        provisioning = props.get("provisioningState")
        running = props.get("runningStatus")
        if provisioning == "Failed":
            state = ComputeState.DEGRADED
            notes.append("Azure reports provisioningState Failed.")
        elif provisioning in ("InProgress", "Deleting"):
            state = ComputeState.DEGRADED
        else:
            state = _RUNNING.get(str(running), ComputeState.UNKNOWN)
        scale = (props.get("template") or {}).get("scale") or {}
        if state == ComputeState.ONLINE and not scale.get("minReplicas"):
            notes.append("minReplicas is 0: the app scales to zero when idle (cold starts).")
        ingress = (props.get("configuration") or {}).get("ingress") or {}
        details.update(
            provisioning_state=provisioning,
            running_status=running,
            min_replicas=scale.get("minReplicas"),
            max_replicas=scale.get("maxReplicas"),
            latest_revision=props.get("latestRevisionName"),
        )
        return Inspection(
            state=state,
            exists=True,
            resources=self._resources(),
            endpoint_host=ingress.get("fqdn"),
            notes=notes,
            details=details,
        )

    # ------------------------------------------------------------------ lifecycle

    def _body(self) -> dict[str, Any]:
        c = self.cfg
        env: list[dict[str, Any]] = [{"name": k, "value": v} for k, v in c.env.items()]
        secrets = []
        for name in c.env_secret_refs:
            if name not in self._secrets:
                raise ComputeError(f"secret for env var {name!r} is not available")
            secrets.append({"name": _secret_name(name), "value": self._secrets[name].reveal()})
            env.append({"name": name, "secretRef": _secret_name(name)})
        container: dict[str, Any] = {
            "name": "inference",
            "image": c.image,
            "env": env,
            "resources": {"cpu": c.cpu, "memory": c.memory},
        }
        if c.command:
            container["command"] = c.command
        if c.args:
            container["args"] = c.args
        properties: dict[str, Any] = {
            "environmentId": c.managed_environment_id,
            "configuration": {
                "ingress": {
                    "external": c.external_ingress,
                    "targetPort": c.container_port,
                    "transport": "auto",
                },
                "secrets": secrets,
            },
            "template": {
                "containers": [container],
                "scale": {"minReplicas": c.min_replicas, "maxReplicas": c.max_replicas},
            },
        }
        if c.workload_profile_name:
            properties["workloadProfileName"] = c.workload_profile_name
        return {
            "location": c.location,
            "tags": {OWNER_LABEL: "true", ENVIRONMENT_LABEL: self.environment_id},
            "properties": properties,
        }

    async def _owned_app(self) -> dict[str, Any]:
        app = await self._get()
        if app is None:
            raise ComputeError(
                f"container app {self.cfg.app_name!r} is not provisioned",
                details={"hint": "run provision first"},
            )
        self._check_owned(app)
        return app

    async def _do_provision(self) -> None:
        existing = await self._get()
        if existing is not None:
            self._check_owned(existing)
            return  # idempotent
        await self.api.request("PUT", self._path, params=self._params, json=self._body())

    async def _do_start(self) -> None:
        await self._owned_app()
        await self.api.request("POST", f"{self._path}/start", params=self._params)

    async def _do_stop(self) -> None:
        await self._owned_app()
        await self.api.request("POST", f"{self._path}/stop", params=self._params)

    async def _do_deprovision(self) -> None:
        if await self._get() is None:
            return
        await self._owned_app()
        await self.api.request("DELETE", self._path, params=self._params)
