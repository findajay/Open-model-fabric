"""AWS EC2 compute adapter: a (GPU) virtual machine that runs an inference server.

Two modes:

* **Managed instance** (no ``instance_id``): ``provision`` launches an instance from
  ``image_id``/``instance_type`` (or a launch template) tagged ``omf-managed=true`` and
  ``omf-environment=<id>``. Later operations find it by those tags. ``deprovision``
  terminates it.
* **Designated instance** (``instance_id`` set): the operator designates an existing
  instance. ``start``/``stop`` are allowed, ``provision`` is a no-op, and ``deprovision``
  (terminate) is refused unless the instance carries the OMF ownership tags.

Use ``user_data`` (cloud-init) to start the inference server at boot, e.g. a vLLM
container. Resources (vCPUs, memory, GPU model and memory) come from
``DescribeInstanceTypes``. Install with ``pip install 'open-model-fabric[s3]'`` (boto3).
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import Field, model_validator

from omf.adapters.compute._managed import (
    ENVIRONMENT_LABEL,
    OWNER_LABEL,
    Inspection,
    ManagedComputeAdapter,
    ManagedComputeConfig,
    owned,
)
from omf.core.domain.errors import ComputeError, ConfigurationError, Conflict
from omf.core.domain.models import ComputeResources, ComputeState, GpuInfo
from omf.core.ports.secrets import SecretRefStr, SecretValue
from omf.core.security.egress import EgressPolicy

ADAPTER_TYPE = "aws_ec2"
_STATE = {
    "running": ComputeState.ONLINE,
    "pending": ComputeState.DEGRADED,
    "stopping": ComputeState.DEGRADED,
    "shutting-down": ComputeState.DEGRADED,
    "stopped": ComputeState.OFFLINE,
    "terminated": ComputeState.OFFLINE,
}


class AwsEc2ComputeConfig(ManagedComputeConfig):
    region: str
    instance_id: str | None = Field(default=None, pattern=r"^i-[0-9a-f]{8,17}$")
    image_id: str | None = Field(default=None, pattern=r"^ami-[0-9a-f]{8,17}$")
    instance_type: str | None = Field(default=None, examples=["g5.xlarge", "g6.xlarge"])
    launch_template_id: str | None = None
    launch_template_version: str = "$Default"
    subnet_id: str | None = None
    security_group_ids: list[str] = Field(default_factory=list)
    key_name: str | None = None
    iam_instance_profile_arn: str | None = None
    user_data: str | None = Field(default=None, max_length=16_000)
    root_volume_gb: int | None = Field(default=None, ge=8, le=16_384)
    spot: bool = False
    use_public_ip: bool = False
    access_key_id_ref: SecretRefStr | None = None
    secret_access_key_ref: SecretRefStr | None = None
    endpoint_url: str | None = None
    """Override the EC2 API endpoint (LocalStack, tests)."""

    @model_validator(mode="after")
    def _launchable(self) -> AwsEc2ComputeConfig:
        if (
            self.instance_id is None
            and not self.launch_template_id
            and not (self.image_id and self.instance_type)
        ):
            raise ValueError("set instance_id, launch_template_id, or image_id + instance_type")
        return self


class AwsEc2ComputeAdapter(ManagedComputeAdapter):
    kind = ADAPTER_TYPE

    def __init__(
        self,
        environment_id: str,
        config: AwsEc2ComputeConfig,
        egress: EgressPolicy,
        *,
        client: Any = None,
        access_key: SecretValue | None = None,
        secret_key: SecretValue | None = None,
    ) -> None:
        super().__init__(environment_id, config, egress)
        self.cfg = config
        self.ephemeral = config.spot
        if client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - optional extra
                raise ConfigurationError(
                    "the aws_ec2 compute adapter needs boto3 (pip install 'open-model-fabric[s3]')"
                ) from exc
            kwargs: dict[str, Any] = {
                "region_name": config.region,
                "endpoint_url": config.endpoint_url,
            }
            if access_key and secret_key:
                kwargs.update(
                    aws_access_key_id=access_key.reveal(), aws_secret_access_key=secret_key.reveal()
                )
            client = boto3.client("ec2", **kwargs)
        self._ec2 = client
        self._types: dict[str, ComputeResources] = {}

    async def _call(self, name: str, **kwargs: Any) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            try:
                return dict(getattr(self._ec2, name)(**kwargs))
            except Exception as exc:
                code = getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)
                message = getattr(exc, "response", {}).get("Error", {}).get("Message", str(exc))
                raise ComputeError(
                    f"EC2 {name} failed: {code}: {str(message)[:300]}",
                    details={"aws_error": code},
                    retryable=code
                    in (
                        "RequestLimitExceeded",
                        "InsufficientInstanceCapacity",
                        "ServiceUnavailable",
                    ),
                ) from exc

        return await asyncio.to_thread(run)

    # ------------------------------------------------------------------ lookup

    async def _instance(self) -> dict[str, Any] | None:
        if self.cfg.instance_id:
            result = await self._call("describe_instances", InstanceIds=[self.cfg.instance_id])
        else:
            result = await self._call(
                "describe_instances",
                Filters=[
                    {"Name": f"tag:{OWNER_LABEL}", "Values": ["true"]},
                    {"Name": f"tag:{ENVIRONMENT_LABEL}", "Values": [self.environment_id]},
                    {
                        "Name": "instance-state-name",
                        "Values": ["pending", "running", "stopping", "stopped"],
                    },
                ],
            )
        instances = [i for r in result.get("Reservations", []) for i in r.get("Instances", [])]
        if len(instances) > 1:
            raise Conflict(
                f"{len(instances)} instances are tagged for environment "
                f"{self.environment_id!r}; expected at most one",
                details={"instance_ids": [i["InstanceId"] for i in instances]},
            )
        return instances[0] if instances else None

    @staticmethod
    def _tags(instance: dict[str, Any]) -> dict[str, str]:
        return {t["Key"]: t["Value"] for t in instance.get("Tags", [])}

    async def _type_resources(self, instance_type: str) -> ComputeResources:
        if instance_type not in self._types:
            info = (await self._call("describe_instance_types", InstanceTypes=[instance_type]))[
                "InstanceTypes"
            ][0]
            gpus = []
            for gpu in (info.get("GpuInfo") or {}).get("Gpus", []):
                memory = (gpu.get("MemoryInfo") or {}).get("SizeInMiB")
                gpus += [
                    GpuInfo(
                        name=f"{gpu.get('Manufacturer', '')} {gpu.get('Name', 'GPU')}".strip(),
                        memory_total_bytes=memory * 2**20 if memory else None,
                    )
                    for _ in range(int(gpu.get("Count", 1)))
                ]
            arch = ((info.get("ProcessorInfo") or {}).get("SupportedArchitectures") or [None])[0]
            memory_mib = (info.get("MemoryInfo") or {}).get("SizeInMiB")
            self._types[instance_type] = ComputeResources(
                cpu_count=(info.get("VCpuInfo") or {}).get("DefaultVCpus"),
                memory_total_bytes=memory_mib * 2**20 if memory_mib else None,
                architecture=arch,
                gpus=gpus,
                accelerator="cuda"
                if any("NVIDIA" in g.name for g in gpus)
                else ("unknown" if gpus else "none"),
                source="detected",
            )
        return self._types[instance_type]

    async def _inspect(self) -> Inspection:
        instance = await self._instance()
        details: dict[str, Any] = {"region": self.cfg.region}
        if instance is None:
            resources = (
                await self._type_resources(self.cfg.instance_type)
                if self.cfg.instance_type
                else None
            )
            return Inspection(
                state=ComputeState.OFFLINE, exists=False, resources=resources, details=details
            )
        state_name = instance.get("State", {}).get("Name", "")
        notes = []
        if not owned(self._tags(instance), self.environment_id):
            notes.append(
                "Designated instance without OMF ownership tags: start/stop allowed, "
                "deprovision (terminate) refused."
            )
        details.update(
            instance_id=instance["InstanceId"],
            instance_type=instance.get("InstanceType"),
            instance_state=state_name,
            availability_zone=instance.get("Placement", {}).get("AvailabilityZone"),
            lifecycle=instance.get("InstanceLifecycle", "on-demand"),
        )
        host = (
            instance.get("PublicIpAddress")
            if self.cfg.use_public_ip
            else instance.get("PrivateIpAddress")
        )
        return Inspection(
            state=_STATE.get(state_name, ComputeState.UNKNOWN),
            exists=True,
            resources=await self._type_resources(instance["InstanceType"]),
            endpoint_host=host,
            notes=notes,
            details=details,
        )

    # ------------------------------------------------------------------ lifecycle

    async def _do_provision(self) -> None:
        if await self._instance() is not None:
            return  # idempotent (designated or already launched)
        if self.cfg.instance_id:
            raise ComputeError(f"designated instance {self.cfg.instance_id} not found")
        c = self.cfg
        tags = [
            {"Key": OWNER_LABEL, "Value": "true"},
            {"Key": ENVIRONMENT_LABEL, "Value": self.environment_id},
            {"Key": "Name", "Value": f"omf-{self.environment_id}"},
        ]
        params: dict[str, Any] = {
            "MinCount": 1,
            "MaxCount": 1,
            "TagSpecifications": [
                {"ResourceType": "instance", "Tags": tags},
                {"ResourceType": "volume", "Tags": tags},
            ],
        }
        if c.launch_template_id:
            params["LaunchTemplate"] = {
                "LaunchTemplateId": c.launch_template_id,
                "Version": c.launch_template_version,
            }
        if c.image_id:
            params["ImageId"] = c.image_id
        if c.instance_type:
            params["InstanceType"] = c.instance_type
        if c.subnet_id:
            params["SubnetId"] = c.subnet_id
        if c.security_group_ids:
            params["SecurityGroupIds"] = c.security_group_ids
        if c.key_name:
            params["KeyName"] = c.key_name
        if c.iam_instance_profile_arn:
            params["IamInstanceProfile"] = {"Arn": c.iam_instance_profile_arn}
        if c.user_data:
            params["UserData"] = c.user_data
        if c.root_volume_gb:
            params["BlockDeviceMappings"] = [
                {
                    "DeviceName": "/dev/sda1",
                    "Ebs": {
                        "VolumeSize": c.root_volume_gb,
                        "VolumeType": "gp3",
                        "DeleteOnTermination": True,
                    },
                }
            ]
        if c.spot:
            params["InstanceMarketOptions"] = {"MarketType": "spot"}
        await self._call("run_instances", **params)

    async def _require(self) -> dict[str, Any]:
        instance = await self._instance()
        if instance is None:
            raise ComputeError(
                "no instance is provisioned for this environment",
                details={"hint": "run provision first"},
            )
        return instance

    async def _do_start(self) -> None:
        instance = await self._require()
        await self._call("start_instances", InstanceIds=[instance["InstanceId"]])

    async def _do_stop(self) -> None:
        instance = await self._require()
        await self._call("stop_instances", InstanceIds=[instance["InstanceId"]])

    async def _do_deprovision(self) -> None:
        instance = await self._instance()
        if instance is None:
            return
        if not owned(self._tags(instance), self.environment_id):
            raise Conflict(
                f"instance {instance['InstanceId']} was not created by Open Model Fabric; "
                "refusing to terminate it",
                details={"instance_id": instance["InstanceId"]},
            )
        await self._call("terminate_instances", InstanceIds=[instance["InstanceId"]])
