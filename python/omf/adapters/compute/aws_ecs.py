"""AWS ECS adapter: an OMF-managed ECS service running an inference container.

* ``provision`` registers a task definition and creates the service with
  ``desiredCount: 0``. Both are tagged ``omf-managed=true`` / ``omf-environment=<id>``.
* ``start`` and ``stop`` set the service's desired count to ``desired_count`` and to 0.
* ``deprovision`` deletes the service (force) and deregisters its task definition, but
  only if both carry the OMF tags. A service with the configured name but without the
  tags is never modified.

Launch types: ``FARGATE`` (CPU only; Fargate has no GPUs) or ``EC2`` with a GPU-capable
capacity (``gpus`` adds a ``GPU`` resource requirement). Networking uses ``awsvpc``.
Secrets are passed as ARNs (Secrets Manager or SSM Parameter Store); ECS resolves them in
the task, so values never pass through OMF. ECS has no stable per-task address, so the
endpoint is ``endpoint_host`` (for example the DNS name of a load balancer in front of
the service). Resources are the task definition's *requested* CPU, memory and GPUs, not
detected hardware.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from omf.adapters.compute._aws import boto_client, call
from omf.adapters.compute._managed import (
    ENVIRONMENT_LABEL,
    OWNER_LABEL,
    Inspection,
    ManagedComputeAdapter,
    ManagedComputeConfig,
    owned,
    pinned_image,
)
from omf.core.domain.errors import ComputeError, Conflict
from omf.core.domain.models import ComputeResources, ComputeState, GpuInfo
from omf.core.ports.secrets import SecretRefStr, SecretValue
from omf.core.security.egress import EgressPolicy

ADAPTER_TYPE = "aws_ecs"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,254}$")


class AwsEcsComputeConfig(ManagedComputeConfig):
    region: str
    cluster: str = Field(description="Cluster name or ARN (must exist)")
    service_name: str
    task_family: str | None = None
    """Task definition family; defaults to ``omf-<service_name>``."""
    image: str
    command: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    env_secret_arn_refs: dict[str, str] = Field(
        default_factory=dict,
        description="Env var name -> Secrets Manager / SSM parameter ARN (resolved by ECS)",
    )
    container_port: int = Field(default=8000, ge=1, le=65535)
    launch_type: Literal["FARGATE", "EC2"] = "FARGATE"
    cpu: str = Field(default="1024", description="Task CPU units (1024 = 1 vCPU)")
    memory: str = Field(default="4096", description="Task memory in MiB")
    gpus: int = Field(default=0, ge=0, le=8)
    desired_count: int = Field(default=1, ge=1, le=100)
    execution_role_arn: str | None = None
    task_role_arn: str | None = None
    subnets: list[str] = Field(default_factory=list)
    security_group_ids: list[str] = Field(default_factory=list)
    assign_public_ip: bool = False
    endpoint_host: str | None = None
    """DNS name or IP that reaches the service (e.g. an ALB); ECS has no stable address."""
    access_key_id_ref: SecretRefStr | None = None
    secret_access_key_ref: SecretRefStr | None = None
    endpoint_url: str | None = None
    """Override the ECS API endpoint (LocalStack, tests)."""

    @field_validator("service_name", "task_family")
    @classmethod
    def _names(cls, v: str | None) -> str | None:
        if v is not None and not _NAME.match(v):
            raise ValueError("must be 1-255 letters, digits, hyphens or underscores")
        return v

    @field_validator("image")
    @classmethod
    def _pinned(cls, v: str) -> str:
        return pinned_image(v)

    @field_validator("env_secret_arn_refs")
    @classmethod
    def _arns(cls, v: dict[str, str]) -> dict[str, str]:
        for name, arn in v.items():
            if not arn.startswith("arn:"):
                raise ValueError(
                    f"env_secret_arn_refs[{name!r}] must be an ARN, not a secret value"
                )
        return v

    @model_validator(mode="after")
    def _check(self) -> AwsEcsComputeConfig:
        if self.gpus and self.launch_type == "FARGATE":
            raise ValueError("Fargate has no GPUs; use launch_type: EC2 with GPU instances")
        if self.launch_type == "FARGATE" and not self.subnets:
            raise ValueError("Fargate (awsvpc) needs at least one subnet")
        if self.endpoint_port is None:
            self.endpoint_port = self.container_port
        return self

    @property
    def family(self) -> str:
        return self.task_family or f"omf-{self.service_name}"


class AwsEcsComputeAdapter(ManagedComputeAdapter):
    kind = ADAPTER_TYPE

    def __init__(
        self,
        environment_id: str,
        config: AwsEcsComputeConfig,
        egress: EgressPolicy,
        *,
        client: Any = None,
        access_key: SecretValue | None = None,
        secret_key: SecretValue | None = None,
    ) -> None:
        super().__init__(environment_id, config, egress)
        self.cfg = config
        self._ecs = client or boto_client(
            "ecs",
            region=config.region,
            endpoint_url=config.endpoint_url,
            access_key=access_key,
            secret_key=secret_key,
            adapter=ADAPTER_TYPE,
        )

    async def _call(self, name: str, **kwargs: Any) -> dict[str, Any]:
        return await call(self._ecs, "ECS", name, **kwargs)

    # ------------------------------------------------------------------ helpers

    def _tags(self) -> list[dict[str, str]]:
        return [
            {"key": OWNER_LABEL, "value": "true"},
            {"key": ENVIRONMENT_LABEL, "value": self.environment_id},
        ]

    @staticmethod
    def _tag_map(tags: list[dict[str, str]] | None) -> dict[str, str]:
        return {t["key"]: t["value"] for t in tags or []}

    async def _service(self) -> dict[str, Any] | None:
        """The live service, or None. Deleted services linger as INACTIVE; they don't count."""
        result = await self._call(
            "describe_services",
            cluster=self.cfg.cluster,
            services=[self.cfg.service_name],
            include=["TAGS"],
        )
        for service in result.get("services") or []:
            if service.get("status") != "INACTIVE":
                return dict(service)
        return None

    def _check_owned(self, service: dict[str, Any]) -> None:
        if not owned(self._tag_map(service.get("tags")), self.environment_id):
            raise Conflict(
                f"ECS service {self.cfg.service_name!r} in {self.cfg.cluster!r} is not managed "
                f"by Open Model Fabric environment {self.environment_id!r}; refusing to touch it"
            )

    def _resources(self) -> ComputeResources:
        c = self.cfg
        gpus = [GpuInfo(name="GPU (ECS resource requirement)") for _ in range(c.gpus)]
        return ComputeResources(
            cpu_count=max(1, int(c.cpu) // 1024) if c.cpu.isdigit() else None,
            memory_total_bytes=int(c.memory) * 2**20 if c.memory.isdigit() else None,
            gpus=gpus,
            accelerator="cuda" if gpus else "none",
            source="declared",
        )

    # ------------------------------------------------------------------ inspection

    async def _inspect(self) -> Inspection:
        service = await self._service()
        details: dict[str, Any] = {
            "region": self.cfg.region,
            "cluster": self.cfg.cluster,
            "service": self.cfg.service_name,
            "launch_type": self.cfg.launch_type,
        }
        notes = ["Resources are the task definition's requested CPU, memory and GPUs."]
        if service is None:
            return Inspection(
                state=ComputeState.OFFLINE,
                exists=False,
                resources=self._resources(),
                notes=notes,
                details=details,
            )
        if not owned(self._tag_map(service.get("tags")), self.environment_id):
            notes.append(
                "A service with this name exists but is not managed by Open Model Fabric; "
                "lifecycle operations will refuse to touch it."
            )
        desired = int(service.get("desiredCount") or 0)
        running = int(service.get("runningCount") or 0)
        rollouts = [d.get("rolloutState") for d in service.get("deployments") or []]
        if service.get("status") == "DRAINING":
            state = ComputeState.DEGRADED
        elif desired == 0:
            state = ComputeState.OFFLINE if running == 0 else ComputeState.DEGRADED
        elif running >= desired and "IN_PROGRESS" not in rollouts:
            state = ComputeState.ONLINE
        else:
            state = ComputeState.DEGRADED
        if "FAILED" in rollouts:
            notes.append("The latest deployment failed; check the ECS service events.")
        details.update(
            desired_count=desired,
            running_count=running,
            pending_count=int(service.get("pendingCount") or 0),
            task_definition=service.get("taskDefinition"),
        )
        return Inspection(
            state=state,
            exists=True,
            resources=self._resources(),
            endpoint_host=self.cfg.endpoint_host,
            notes=notes,
            details=details,
        )

    # ------------------------------------------------------------------ lifecycle

    def _task_definition(self) -> dict[str, Any]:
        c = self.cfg
        container: dict[str, Any] = {
            "name": "inference",
            "image": c.image,
            "essential": True,
            "portMappings": [{"containerPort": c.container_port, "protocol": "tcp"}],
            "environment": [{"name": k, "value": v} for k, v in c.env.items()],
            "secrets": [{"name": k, "valueFrom": v} for k, v in c.env_secret_arn_refs.items()],
        }
        if c.command:
            container["command"] = c.command
        if c.gpus:
            container["resourceRequirements"] = [{"type": "GPU", "value": str(c.gpus)}]
        params: dict[str, Any] = {
            "family": c.family,
            "networkMode": "awsvpc",
            "requiresCompatibilities": [c.launch_type],
            "cpu": c.cpu,
            "memory": c.memory,
            "containerDefinitions": [container],
            "tags": self._tags(),
        }
        if c.execution_role_arn:
            params["executionRoleArn"] = c.execution_role_arn
        if c.task_role_arn:
            params["taskRoleArn"] = c.task_role_arn
        return params

    async def _do_provision(self) -> None:
        existing = await self._service()
        if existing is not None:
            self._check_owned(existing)
            return  # idempotent
        c = self.cfg
        task = await self._call("register_task_definition", **self._task_definition())
        params: dict[str, Any] = {
            "cluster": c.cluster,
            "serviceName": c.service_name,
            "taskDefinition": task["taskDefinition"]["taskDefinitionArn"],
            "desiredCount": 0,
            "launchType": c.launch_type,
            "tags": self._tags(),
            "propagateTags": "SERVICE",
            "enableECSManagedTags": True,
        }
        if c.subnets:
            params["networkConfiguration"] = {
                "awsvpcConfiguration": {
                    "subnets": c.subnets,
                    "securityGroups": c.security_group_ids,
                    "assignPublicIp": "ENABLED" if c.assign_public_ip else "DISABLED",
                }
            }
        await self._call("create_service", **params)

    async def _scale(self, count: int) -> None:
        service = await self._service()
        if service is None:
            raise ComputeError(
                f"ECS service {self.cfg.service_name!r} is not provisioned",
                details={"hint": "run provision first"},
            )
        self._check_owned(service)
        await self._call(
            "update_service",
            cluster=self.cfg.cluster,
            service=self.cfg.service_name,
            desiredCount=count,
        )

    async def _do_start(self) -> None:
        await self._scale(self.cfg.desired_count)

    async def _do_stop(self) -> None:
        await self._scale(0)

    async def _do_deprovision(self) -> None:
        service = await self._service()
        if service is None:
            return
        self._check_owned(service)
        await self._call(
            "delete_service", cluster=self.cfg.cluster, service=self.cfg.service_name, force=True
        )
        task_arn = service.get("taskDefinition")
        if task_arn:
            task = await self._call(
                "describe_task_definition", taskDefinition=task_arn, include=["TAGS"]
            )
            if owned(self._tag_map(task.get("tags")), self.environment_id):
                await self._call("deregister_task_definition", taskDefinition=task_arn)
