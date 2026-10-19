"""Docker compute adapter: one OMF-managed container running an inference server.

Talks to the Docker Engine API directly (Unix socket or TCP) with httpx; no Docker SDK.
``provision`` pulls the pinned image and creates the container, ``start``/``stop`` run and
stop it, ``deprovision`` removes it. Typical images: ``ollama/ollama``,
``vllm/vllm-openai`` (GPU via ``gpus``), ``ghcr.io/ggml-org/llama.cpp:server``.

Security notes:
* Access to the Docker socket is root-equivalent on that host. Enable this adapter only
  where the API process is meant to manage containers, and prefer a socket proxy that
  only allows the container endpoints used here.
* Only named volumes can be mounted (no host bind mounts), and ports bind to
  ``127.0.0.1`` unless configured otherwise.
* Secret environment values (``env_secret_refs``) are resolved by the backend and stored
  in the container configuration, where ``docker inspect`` can read them.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import httpx
from pydantic import Field, field_validator, model_validator

from omf.adapters.compute._managed import (
    ENVIRONMENT_LABEL,
    OWNER_LABEL,
    Inspection,
    ManagedComputeAdapter,
    ManagedComputeConfig,
    owned,
)
from omf.adapters.compute._rest import RestClient
from omf.core.domain.errors import ComputeError, Conflict
from omf.core.domain.models import ComputeResources, ComputeState
from omf.core.ports.secrets import SecretRefStr, SecretValue
from omf.core.security.egress import EgressPolicy

ADAPTER_TYPE = "docker"
_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
_VOLUME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*:/[^:]*$")


class DockerComputeConfig(ManagedComputeConfig):
    docker_host: str = "unix:///var/run/docker.sock"
    container_name: str
    image: str = Field(description="Pinned image reference: name:tag or name@sha256:digest")
    command: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    env_secret_refs: dict[str, SecretRefStr] = Field(default_factory=dict)
    container_port: int | None = Field(default=None, ge=1, le=65535)
    host_port: int | None = Field(default=None, ge=1, le=65535)
    host_ip: str = "127.0.0.1"
    network_mode: Literal["bridge", "host"] = "bridge"
    gpus: int | Literal["all"] = 0
    volumes: list[str] = Field(default_factory=list, description="Named volumes: 'name:/path'")
    registry_auth_ref: SecretRefStr | None = None
    """Secret holding the registry auth JSON ({"username", "password", "serveraddress"})."""
    stop_timeout_s: int = Field(default=30, ge=0, le=600)
    pull_policy: Literal["if_not_present", "always"] = "if_not_present"
    """Pull only when the image is missing (as Kubernetes does for pinned tags)."""

    @field_validator("container_name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _NAME.match(v):
            raise ValueError("invalid container name")
        return v

    @field_validator("image")
    @classmethod
    def _pinned(cls, v: str) -> str:
        last = v.rsplit("/", 1)[-1]
        if "@sha256:" not in v and ":" not in last:
            raise ValueError("image must be pinned to a tag or digest (e.g. ollama/ollama:0.40.6)")
        if last.endswith(":latest"):
            raise ValueError("':latest' is not reproducible; pin a version tag or digest")
        return v

    @field_validator("volumes")
    @classmethod
    def _named_volumes(cls, v: list[str]) -> list[str]:
        for item in v:
            if not _VOLUME.match(item):
                raise ValueError(f"{item!r}: only named volumes 'name:/container/path' are allowed")
        return v

    @field_validator("docker_host")
    @classmethod
    def _host(cls, v: str) -> str:
        if not v.startswith(("unix:///", "http://", "https://")):
            raise ValueError(
                "docker_host must be unix:///path, http://host:port or https://host:port"
            )
        return v

    @model_validator(mode="after")
    def _ports(self) -> DockerComputeConfig:
        if self.network_mode == "bridge" and self.container_port and self.host_port is None:
            self.host_port = self.container_port
        if self.endpoint_port is None:
            self.endpoint_port = (
                self.container_port if self.network_mode == "host" else self.host_port
            )
        return self


class DockerComputeAdapter(ManagedComputeAdapter):
    kind = ADAPTER_TYPE

    def __init__(
        self,
        environment_id: str,
        config: DockerComputeConfig,
        egress: EgressPolicy,
        *,
        secrets: dict[str, SecretValue] | None = None,
        registry_auth: SecretValue | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        probe_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(environment_id, config, egress, probe_transport=probe_transport)
        self.cfg = config
        self._secrets = secrets or {}
        self._registry_auth = registry_auth
        if config.docker_host.startswith("unix://"):
            transport = transport or httpx.AsyncHTTPTransport(
                uds=config.docker_host[len("unix://") :]
            )
            base_url = "http://docker"
            self._endpoint_host = (
                "127.0.0.1" if config.host_ip in ("0.0.0.0", "") else config.host_ip  # noqa: S104
            )
        else:
            base_url = config.docker_host
            self._endpoint_host = urlsplit(config.docker_host).hostname or "127.0.0.1"
        self.api = RestClient(
            name=f"Docker Engine ({config.docker_host})",
            base_url=base_url,
            transport=transport,
            timeout_s=config.operation_timeout_s,
        )

    async def aclose(self) -> None:
        await self.api.aclose()

    # ------------------------------------------------------------------ inspection

    async def _container(self) -> dict[str, Any] | None:
        name = quote(self.cfg.container_name)
        result: dict[str, Any] | None = await self.api.request(
            "GET", f"/containers/{name}/json", allow_404=True
        )
        return result

    def _owned_or_conflict(self, container: dict[str, Any]) -> None:
        if not owned(container.get("Config", {}).get("Labels"), self.environment_id):
            raise Conflict(
                f"container {self.cfg.container_name!r} exists but is not managed by Open Model "
                f"Fabric environment {self.environment_id!r}; refusing to touch it",
                details={"container": self.cfg.container_name},
            )

    async def _inspect(self) -> Inspection:
        info: dict[str, Any] = await self.api.request("GET", "/info")
        runtimes = info.get("Runtimes") or {}
        wants_gpu = self.cfg.gpus != 0
        resources = ComputeResources(
            cpu_count=info.get("NCPU"),
            memory_total_bytes=info.get("MemTotal"),
            architecture=info.get("Architecture"),
            accelerator="cuda" if wants_gpu and "nvidia" in runtimes else "none",
            source="detected",
        )
        details: dict[str, Any] = {
            "docker_version": info.get("ServerVersion"),
            "os": info.get("OperatingSystem"),
            "container": self.cfg.container_name,
            "image": self.cfg.image,
            "gpu_runtime": "nvidia" in runtimes,
        }
        notes: list[str] = []
        if wants_gpu and "nvidia" not in runtimes:
            notes.append(
                "GPUs requested but the NVIDIA container runtime is not installed on this host."
            )
        container = await self._container()
        if container is None:
            return Inspection(
                state=ComputeState.OFFLINE,
                exists=False,
                resources=resources,
                details=details,
                notes=notes,
            )
        if not owned(container.get("Config", {}).get("Labels"), self.environment_id):
            notes.append(
                "A container with this name exists but is not managed by Open Model "
                "Fabric; lifecycle operations will refuse to touch it."
            )
        state_info = container.get("State", {})
        status = state_info.get("Status")
        health = (state_info.get("Health") or {}).get("Status")
        if status == "running":
            state = ComputeState.DEGRADED if health == "unhealthy" else ComputeState.ONLINE
        elif status in ("restarting", "removing"):
            state = ComputeState.DEGRADED
        else:  # created, exited, paused, dead
            state = ComputeState.OFFLINE
        details.update(
            container_id=container.get("Id", "")[:12],
            container_status=status,
            health=health,
            started_at=state_info.get("StartedAt"),
        )
        return Inspection(
            state=state,
            exists=True,
            resources=resources,
            endpoint_host=self._endpoint_host,
            details=details,
            notes=notes,
        )

    # ------------------------------------------------------------------ lifecycle

    async def _pull(self) -> None:
        headers = {}
        if self._registry_auth is not None:
            headers["X-Registry-Auth"] = base64.urlsafe_b64encode(
                self._registry_auth.reveal().encode()
            ).decode()
        async with self.api.client.stream(
            "POST", "/images/create", params={"fromImage": self.cfg.image}, headers=headers
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                raise self.api.error(response)
            async for line in response.aiter_lines():
                if line.strip():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if "error" in event:  # pull errors arrive in-band with HTTP 200
                        raise ComputeError(f"image pull failed: {str(event['error'])[:300]}")

    def _create_body(self) -> dict[str, Any]:
        c = self.cfg
        env = [f"{k}={v}" for k, v in c.env.items()]
        env += [f"{k}={self._secrets[k].reveal()}" for k in c.env_secret_refs if k in self._secrets]
        host: dict[str, Any] = {
            "NetworkMode": c.network_mode,
            "RestartPolicy": {"Name": "unless-stopped"},
            "Mounts": [
                {"Type": "volume", "Source": v.split(":", 1)[0], "Target": v.split(":", 1)[1]}
                for v in c.volumes
            ],
        }
        body: dict[str, Any] = {
            "Image": c.image,
            "Env": env,
            "Labels": {OWNER_LABEL: "true", ENVIRONMENT_LABEL: self.environment_id},
            "HostConfig": host,
        }
        if c.command:
            body["Cmd"] = c.command
        if c.container_port and c.network_mode == "bridge":
            port = f"{c.container_port}/tcp"
            body["ExposedPorts"] = {port: {}}
            host["PortBindings"] = {port: [{"HostIp": c.host_ip, "HostPort": str(c.host_port)}]}
        if c.gpus:
            host["DeviceRequests"] = [
                {
                    "Driver": "nvidia",
                    "Count": -1 if c.gpus == "all" else c.gpus,
                    "Capabilities": [["gpu"]],
                }
            ]
        return body

    async def _do_provision(self) -> None:
        existing = await self._container()
        if existing is not None:
            self._owned_or_conflict(existing)
            return  # idempotent
        image_path = f"/images/{quote(self.cfg.image, safe='/:@')}/json"
        if (
            self.cfg.pull_policy == "always"
            or await self.api.request("GET", image_path, allow_404=True) is None
        ):
            await self._pull()
        await self.api.request(
            "POST",
            "/containers/create",
            params={"name": self.cfg.container_name},
            json=self._create_body(),
        )

    async def _owned_container(self) -> dict[str, Any]:
        container = await self._container()
        if container is None:
            raise ComputeError(
                f"container {self.cfg.container_name!r} is not provisioned",
                details={"hint": "run provision first"},
            )
        self._owned_or_conflict(container)
        return container

    async def _do_start(self) -> None:
        container = await self._owned_container()
        await self.api.request("POST", f"/containers/{container['Id']}/start", ok=(304,))

    async def _do_stop(self) -> None:
        container = await self._owned_container()
        await self.api.request(
            "POST",
            f"/containers/{container['Id']}/stop",
            params={"t": self.cfg.stop_timeout_s},
            ok=(304,),
        )

    async def _do_deprovision(self) -> None:
        container = await self._container()
        if container is None:
            return
        self._owned_or_conflict(container)
        await self.api.request("DELETE", f"/containers/{container['Id']}", params={"force": "true"})
