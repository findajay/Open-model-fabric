"""ECS, Cloud Run, Azure Container Apps and Azure VM provisioning against fakes.

NOT tested against real clouds: ECS uses moto; Cloud Run and Azure use in-memory fakes of
the REST endpoints the adapters call (``httpx.MockTransport``). The request shapes follow
the Cloud Run v2 discovery document and the Azure REST API specs for the pinned
api-versions. The tests check the ownership rules, state mapping and request bodies.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from omf.adapters.compute.aws_ecs import AwsEcsComputeAdapter, AwsEcsComputeConfig
from omf.adapters.compute.azure_container_apps import (
    AzureContainerAppsComputeAdapter,
    AzureContainerAppsComputeConfig,
)
from omf.adapters.compute.azure_vm import AzureVmComputeAdapter, AzureVmComputeConfig
from omf.adapters.compute.gcp_cloud_run import GcpCloudRunComputeAdapter, GcpCloudRunComputeConfig
from omf.core.domain.errors import ComputeError, Conflict, LifecycleDisabled
from omf.core.domain.models import ComputeOperation, ComputeState
from omf.core.ports.secrets import SecretValue
from omf.core.security.egress import EgressPolicy

EGRESS = EgressPolicy()
MANAGED = {"omf-managed": "true", "omf-environment": "gpu"}
IMAGE = "vllm/vllm-openai:v0.32.0"


async def token() -> str:
    return "test-token"


# ---------------------------------------------------------------------- ECS (moto)


@pytest.fixture
def ecs() -> Iterator[Any]:
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")
    with moto.mock_aws():
        client = boto3.client("ecs", region_name="us-east-1")
        client.create_cluster(clusterName="inference")
        ec2 = boto3.client("ec2", region_name="us-east-1")
        vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
        client.subnet_id = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24")["Subnet"][
            "SubnetId"
        ]
        yield client


def ecs_adapter(client: Any, **overrides: Any) -> AwsEcsComputeAdapter:
    raw: dict[str, Any] = {
        "region": "us-east-1",
        "cluster": "inference",
        "service_name": "vllm",
        "image": IMAGE,
        "launch_type": "EC2",
        "gpus": 1,
        "subnets": [client.subnet_id],
        "env_secret_arn_refs": {
            "HF_TOKEN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:hf"
        },
        "allow_lifecycle": True,
        "endpoint_host": "vllm.internal.example.com",
        "probe_endpoint": False,
    }
    raw.update(overrides)
    return AwsEcsComputeAdapter(
        "gpu", AwsEcsComputeConfig.model_validate(raw), EGRESS, client=client
    )


async def test_ecs_service_lifecycle(ecs: Any) -> None:
    adapter = ecs_adapter(ecs)
    env = await adapter.describe_environment()
    assert env.state == ComputeState.OFFLINE and "Not provisioned." in env.notes
    assert env.resources is not None and env.resources.accelerator == "cuda"
    assert env.resources.source == "declared"

    env = await adapter.provision()
    assert env.state == ComputeState.OFFLINE  # created with desiredCount 0
    service = ecs.describe_services(cluster="inference", services=["vllm"], include=["TAGS"])
    service = service["services"][0]
    assert {t["key"]: t["value"] for t in service["tags"]} == MANAGED
    task = ecs.describe_task_definition(taskDefinition=service["taskDefinition"])
    container = task["taskDefinition"]["containerDefinitions"][0]
    assert container["resourceRequirements"] == [{"value": "1", "type": "GPU"}]
    assert container["secrets"][0]["valueFrom"].startswith("arn:aws:secretsmanager")
    await adapter.provision()  # idempotent

    env = await adapter.start()
    assert env.state == ComputeState.ONLINE and env.details["desired_count"] == 1
    assert env.endpoints[0].url == "http://vllm.internal.example.com:8000"
    assert (await adapter.stop()).state == ComputeState.OFFLINE

    env = await adapter.deprovision()
    assert "Not provisioned." in env.notes
    families = ecs.list_task_definitions(familyPrefix="omf-vllm", status="ACTIVE")
    assert families["taskDefinitionArns"] == []  # deregistered with the service
    await adapter.deprovision()  # idempotent


async def test_ecs_refuses_foreign_service_and_reports_errors(ecs: Any) -> None:
    task = ecs.register_task_definition(
        family="theirs", containerDefinitions=[{"name": "x", "image": "nginx:1.27", "memory": 128}]
    )["taskDefinition"]["taskDefinitionArn"]
    ecs.create_service(cluster="inference", serviceName="vllm", taskDefinition=task, desiredCount=2)
    adapter = ecs_adapter(ecs)
    for op in (adapter.provision, adapter.start, adapter.stop, adapter.deprovision):
        with pytest.raises(Conflict):
            await op()
    service = ecs.describe_services(cluster="inference", services=["vllm"])["services"][0]
    assert service["desiredCount"] == 2 and service["status"] == "ACTIVE"  # untouched

    missing = ecs_adapter(ecs, cluster="nope", service_name="other")
    with pytest.raises(ComputeError, match="ClusterNotFoundException"):
        await missing.describe_environment()
    with pytest.raises(ComputeError, match="not provisioned"):
        await ecs_adapter(ecs, service_name="other").start()


def test_ecs_config_validation() -> None:
    base = {"region": "us-east-1", "cluster": "c", "service_name": "s", "image": IMAGE}
    with pytest.raises(ValidationError, match="Fargate has no GPUs"):
        AwsEcsComputeConfig.model_validate({**base, "gpus": 1, "subnets": ["subnet-1"]})
    with pytest.raises(ValidationError, match="subnet"):
        AwsEcsComputeConfig.model_validate(base)  # Fargate needs awsvpc subnets
    with pytest.raises(ValidationError, match="must be an ARN"):
        AwsEcsComputeConfig.model_validate(
            {**base, "launch_type": "EC2", "env_secret_arn_refs": {"KEY": "plaintext-secret"}}
        )
    with pytest.raises(ValidationError, match="pinned"):
        AwsEcsComputeConfig.model_validate({**base, "launch_type": "EC2", "image": "vllm"})


# ---------------------------------------------------------------------- Cloud Run


class FakeCloudRun:
    """Cloud Run v2 services + long-running operations (one poll before done)."""

    PARENT = "/v2/projects/omf-test-project/locations/us-central1"

    def __init__(self, *, fail_create: bool = False) -> None:
        self.service: dict[str, Any] | None = None
        self.fail_create = fail_create
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.pending: dict[str, dict[str, Any]] = {}

    def _op(self, name: str, error: dict[str, Any] | None = None) -> httpx.Response:
        full = f"projects/omf-test-project/locations/us-central1/operations/{name}"
        self.pending[full] = {"name": full, "done": True, **({"error": error} if error else {})}
        return httpx.Response(200, json={"name": full, "done": False})

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-token"
        body = json.loads(request.content) if request.content else None
        path, method = request.url.path, request.method
        self.calls.append((method, path, body))
        service_path = f"{self.PARENT}/services/llm"
        if "/operations/" in path:
            return httpx.Response(200, json=self.pending[path.removeprefix("/v2/")])
        if path == f"{self.PARENT}/services" and method == "POST":
            assert request.url.params["serviceId"] == "llm"
            if self.fail_create:
                return self._op("op-create", {"code": 9, "message": "GPU quota exceeded"})
            self.service = {
                **body,
                "name": "projects/omf-test-project/locations/us-central1/services/llm",
                "uri": "https://llm-abc123-uc.a.run.app",
                "terminalCondition": {"state": "CONDITION_SUCCEEDED"},
                "reconciling": False,
            }
            return self._op("op-create")
        if path == service_path:
            if method == "GET":
                return (
                    httpx.Response(200, json=self.service)
                    if self.service
                    else httpx.Response(404, json={"error": {"message": "not found"}})
                )
            if method == "PATCH" and self.service:
                assert request.url.params["updateMask"] == "scaling.minInstanceCount"
                self.service["scaling"] = body["scaling"]
                return self._op("op-patch")
            if method == "DELETE" and self.service:
                self.service = None
                return self._op("op-delete")
        return httpx.Response(404, json={"error": {"message": f"unexpected {method} {path}"}})


def cloud_run(fake: FakeCloudRun, **overrides: Any) -> GcpCloudRunComputeAdapter:
    raw: dict[str, Any] = {
        "project": "omf-test-project",
        "region": "us-central1",
        "service": "llm",
        "image": IMAGE,
        "args": ["--model", "Qwen/Qwen2.5-0.5B-Instruct"],
        "gpu_type": "nvidia-l4",
        "env_secret_manager_refs": {"HF_TOKEN": "hf-token:3"},
        "allow_lifecycle": True,
        "probe_endpoint": False,
    }
    raw.update(overrides)
    return GcpCloudRunComputeAdapter(
        "gpu",
        GcpCloudRunComputeConfig.model_validate(raw),
        EGRESS,
        token,
        transport=httpx.MockTransport(fake.handler),
    )


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_: float) -> None:
        return None

    monkeypatch.setattr("omf.adapters.compute.gcp_cloud_run.asyncio.sleep", instant)
    monkeypatch.setattr("omf.adapters.compute.azure_vm.asyncio.sleep", instant)


async def test_cloud_run_lifecycle(no_sleep: None) -> None:
    fake = FakeCloudRun()
    adapter = cloud_run(fake)
    env = await adapter.describe_environment()
    assert env.state == ComputeState.OFFLINE and "Not provisioned." in env.notes
    assert env.resources is not None and [g.name for g in env.resources.gpus] == ["nvidia-l4"]

    env = await adapter.provision()
    assert fake.service is not None
    assert fake.service["labels"] == MANAGED
    template = fake.service["template"]
    assert template["nodeSelector"] == {"accelerator": "nvidia-l4"}
    container = template["containers"][0]
    assert container["resources"] == {
        "limits": {"cpu": "4", "memory": "16Gi", "nvidia.com/gpu": "1"},
        "cpuIdle": False,  # GPUs need CPU always allocated
    }
    assert container["env"] == [
        {
            "name": "HF_TOKEN",
            "valueSource": {"secretKeyRef": {"secret": "hf-token", "version": "3"}},
        }
    ]
    assert fake.service["scaling"] == {"minInstanceCount": 0}
    # Scaled to zero: not "online", and the note says it still serves cold starts.
    assert env.state == ComputeState.OFFLINE
    assert any("cold start" in n for n in env.notes)
    assert env.endpoints[0].url == "https://llm-abc123-uc.a.run.app:443"
    assert any(m == "GET" and "/operations/" in p for m, p, _ in fake.calls)  # polled

    assert (await adapter.start()).state == ComputeState.ONLINE
    assert fake.service["scaling"] == {"minInstanceCount": 1}
    assert (await adapter.stop()).state == ComputeState.OFFLINE
    await adapter.deprovision()
    assert fake.service is None
    await adapter.aclose()


async def test_cloud_run_refuses_foreign_service_and_reports_operation_errors(
    no_sleep: None,
) -> None:
    fake = FakeCloudRun()
    fake.service = {"name": "x", "labels": {"team": "theirs"}, "uri": "https://x.a.run.app"}
    adapter = cloud_run(fake)
    for op in (adapter.provision, adapter.start, adapter.stop, adapter.deprovision):
        with pytest.raises(Conflict):
            await op()
    assert not any(m in ("POST", "PATCH", "DELETE") for m, _, _ in fake.calls)

    failing = cloud_run(FakeCloudRun(fail_create=True))
    with pytest.raises(ComputeError, match="GPU quota exceeded"):
        await failing.provision()

    cpu_only = FakeCloudRun()
    await cloud_run(cpu_only, gpu_type=None, cpu="2", memory="4Gi").provision()
    assert cpu_only.service is not None
    assert "nodeSelector" not in cpu_only.service["template"]
    assert cpu_only.service["template"]["containers"][0]["resources"]["cpuIdle"] is True
    with pytest.raises(ValidationError):
        GcpCloudRunComputeConfig.model_validate(
            {
                "project": "omf-test-project",
                "region": "us-central1",
                "service": "llm",
                "image": IMAGE,
                "min_instances": 3,
                "max_instances": 1,
            }
        )


# ---------------------------------------------------------------------- Azure fakes

SUB = "00000000-0000-0000-0000-000000000001"
GROUP = f"/subscriptions/{SUB}/resourceGroups/rg-llm"
APP = f"{GROUP}/providers/Microsoft.App/containerApps/llm-app"
ENV_ID = f"{GROUP}/providers/Microsoft.App/managedEnvironments/llm-env"
VM = f"{GROUP}/providers/Microsoft.Compute/virtualMachines/llm-vm"
NIC = f"{GROUP}/providers/Microsoft.Network/networkInterfaces/llm-vm-nic"
SUBNET = f"{GROUP}/providers/Microsoft.Network/virtualNetworks/vnet/subnets/default"


class FakeArm:
    """Container apps, VMs and NICs in one fake Azure Resource Manager."""

    def __init__(self) -> None:
        self.resources: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.nic_polls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        if path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "aad-token", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer aad-token"
        version = request.url.params["api-version"]
        if "/locations/" in path and path.endswith("/vmSizes"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"name": "Standard_NC4as_T4_v3", "numberOfCores": 4, "memoryInMB": 28672}
                    ]
                },
            )
        base, _, action = path.rpartition("/")
        if (
            method == "POST"
            and action in ("start", "stop", "deallocate")
            and base in self.resources
        ):
            props = self.resources[base]["properties"]
            if "Microsoft.App" in base:
                assert version == "2025-01-01"
                props["runningStatus"] = "Running" if action == "start" else "Stopped"
            else:
                props["instanceView"]["statuses"][1]["code"] = (
                    "PowerState/running" if action == "start" else "PowerState/deallocated"
                )
            return httpx.Response(202)
        if method == "GET":
            if path not in self.resources:
                return httpx.Response(404, json={"error": {"code": "ResourceNotFound"}})
            if path == NIC:
                self.nic_polls += 1
                if self.nic_polls >= 2:  # provisioning finishes after the first poll
                    self.resources[NIC]["properties"]["provisioningState"] = "Succeeded"
            return httpx.Response(200, json=self.resources[path])
        if method == "PUT":
            body = json.loads(request.content)
            if "Microsoft.App" in path:
                assert version == "2025-01-01"
                body["properties"].update(
                    provisioningState="Succeeded",
                    runningStatus="Running",
                    configuration={
                        **body["properties"]["configuration"],
                        "ingress": {
                            **body["properties"]["configuration"]["ingress"],
                            "fqdn": "llm-app.example.azurecontainerapps.io",
                        },
                    },
                )
            elif "networkInterfaces" in path:
                assert version == "2024-07-01"
                body["id"] = path
                body["properties"]["provisioningState"] = "Updating"
                body["properties"]["ipConfigurations"][0]["properties"]["privateIPAddress"] = (
                    "10.0.0.7"
                )
            else:
                assert version == "2024-07-01"
                body["properties"]["instanceView"] = {
                    "statuses": [
                        {"code": "ProvisioningState/succeeded"},
                        {"code": "PowerState/running"},
                    ]
                }
            self.resources[path] = body
            return httpx.Response(201, json=body)
        if method == "DELETE" and path in self.resources:
            del self.resources[path]
            if path == VM:  # deleteOption=Delete removes the NIC (and disk) with the VM
                self.resources.pop(NIC, None)
            return httpx.Response(202)
        return httpx.Response(404, json={"error": {"code": f"unexpected {method} {path}"}})


def credentials(config: Any, transport: httpx.MockTransport) -> Any:
    from omf.adapters.compute.azure_vm import AzureClientCredentials

    return AzureClientCredentials(config, SecretValue("s3cret"), transport=transport)


AZURE = {
    "subscription_id": SUB,
    "resource_group": "rg-llm",
    "tenant_id": "tenant",
    "client_id": "app",
    "client_secret_ref": "env:AZ_SECRET",
    "allow_lifecycle": True,
    "probe_endpoint": False,
}


def container_app(fake: FakeArm, **overrides: Any) -> AzureContainerAppsComputeAdapter:
    raw: dict[str, Any] = {
        **AZURE,
        "app_name": "llm-app",
        "location": "swedencentral",
        "managed_environment_id": ENV_ID,
        "workload_profile_name": "gpu-t4",
        "image": IMAGE,
        "env_secret_refs": {"HF_TOKEN": "env:HF_TOKEN"},
    }
    raw.update(overrides)
    config = AzureContainerAppsComputeConfig.model_validate(raw)
    transport = httpx.MockTransport(fake.handler)
    return AzureContainerAppsComputeAdapter(
        "gpu",
        config,
        EGRESS,
        credentials(config, transport),
        secrets={"HF_TOKEN": SecretValue("hf_secret")},
        transport=transport,
    )


async def test_container_apps_lifecycle() -> None:
    fake = FakeArm()
    adapter = container_app(fake)
    env = await adapter.describe_environment()
    assert env.state == ComputeState.OFFLINE and "Not provisioned." in env.notes

    env = await adapter.provision()
    app = fake.resources[APP]
    assert app["tags"] == MANAGED
    props = app["properties"]
    assert props["environmentId"] == ENV_ID and props["workloadProfileName"] == "gpu-t4"
    assert props["template"]["scale"] == {"minReplicas": 0, "maxReplicas": 1}
    assert props["configuration"]["secrets"] == [{"name": "omf-hf-token", "value": "hf_secret"}]
    assert props["template"]["containers"][0]["env"] == [
        {"name": "HF_TOKEN", "secretRef": "omf-hf-token"}
    ]
    assert env.state == ComputeState.ONLINE
    assert any("scales to zero" in n for n in env.notes)
    assert env.endpoints[0].url == "https://llm-app.example.azurecontainerapps.io:443"
    await adapter.provision()  # idempotent
    assert sum(1 for m, p in fake.calls if m == "PUT" and p == APP) == 1

    assert (await adapter.stop()).state == ComputeState.OFFLINE
    assert (await adapter.start()).state == ComputeState.ONLINE
    await adapter.deprovision()
    assert APP not in fake.resources
    await adapter.aclose()


async def test_container_apps_refuse_foreign_app() -> None:
    fake = FakeArm()
    fake.resources[APP] = {"tags": {"team": "theirs"}, "properties": {"runningStatus": "Running"}}
    adapter = container_app(fake)
    for op in (adapter.provision, adapter.start, adapter.stop, adapter.deprovision):
        with pytest.raises(Conflict):
            await op()
    assert not any(m in ("PUT", "POST", "DELETE") for m, p in fake.calls if p.startswith(APP))
    with pytest.raises(ValidationError):
        container_app(fake, managed_environment_id="/not/an/arm/id")


def vm(fake: FakeArm, **overrides: Any) -> AzureVmComputeAdapter:
    raw: dict[str, Any] = {
        **AZURE,
        "vm_name": "llm-vm",
        "location": "eastus",
        "subnet_id": SUBNET,
        "vm_size": "Standard_NC4as_T4_v3",
        "image": "Canonical:ubuntu-24_04-lts:server:latest",
        "ssh_public_key_ref": "env:SSH_KEY",
        "custom_data": "#cloud-config\nruncmd: [docker run vllm]",
        "endpoint_port": 8000,
    }
    raw.update(overrides)
    config = AzureVmComputeConfig.model_validate(raw)
    transport = httpx.MockTransport(fake.handler)
    return AzureVmComputeAdapter(
        "gpu",
        config,
        EGRESS,
        credentials(config, transport),
        ssh_public_key=SecretValue("ssh-ed25519 AAAAC3Nza test@laptop\n"),
        transport=transport,
    )


async def test_azure_vm_provisioning(no_sleep: None) -> None:
    fake = FakeArm()
    adapter = vm(fake)
    assert ComputeOperation.PROVISION in adapter.supported_operations()
    env = await adapter.provision()
    nic, machine = fake.resources[NIC], fake.resources[VM]
    assert nic["tags"] == MANAGED and machine["tags"] == MANAGED
    assert nic["properties"]["ipConfigurations"][0]["properties"]["subnet"] == {"id": SUBNET}
    props = machine["properties"]
    assert props["storageProfile"]["imageReference"] == {
        "publisher": "Canonical",
        "offer": "ubuntu-24_04-lts",
        "sku": "server",
        "version": "latest",
    }
    assert props["storageProfile"]["osDisk"]["deleteOption"] == "Delete"
    assert props["networkProfile"]["networkInterfaces"][0] == {
        "id": NIC,
        "properties": {"primary": True, "deleteOption": "Delete"},
    }
    linux = props["osProfile"]["linuxConfiguration"]
    assert linux["disablePasswordAuthentication"] is True
    assert linux["ssh"]["publicKeys"][0]["keyData"] == "ssh-ed25519 AAAAC3Nza test@laptop"
    assert base64.b64decode(props["osProfile"]["customData"]).startswith(b"#cloud-config")
    assert fake.nic_polls >= 2  # waited for the NIC before creating the VM
    assert env.state == ComputeState.ONLINE
    assert env.endpoints[0].url.startswith("http://10.0.0.7:")  # NIC private IP

    assert (await adapter.stop()).state == ComputeState.OFFLINE
    await adapter.deprovision()
    assert VM not in fake.resources and NIC not in fake.resources


async def test_azure_vm_designated_and_orphan_nic(no_sleep: None) -> None:
    fake = FakeArm()
    fake.resources[VM] = {
        "location": "eastus",
        "properties": {
            "hardwareProfile": {"vmSize": "Standard_NC4as_T4_v3"},
            "instanceView": {"statuses": [{"code": "x"}, {"code": "PowerState/deallocated"}]},
        },
    }
    adapter = vm(fake, endpoint_host="10.9.9.9")
    env = await adapter.provision()  # designated VM: no-op
    assert any("Designated VM" in n for n in env.notes)
    assert not any(m == "PUT" for m, _ in fake.calls)
    assert (await adapter.start()).state == ComputeState.ONLINE
    with pytest.raises(Conflict):
        await adapter.deprovision()
    assert VM in fake.resources

    orphan = FakeArm()
    orphan.resources[NIC] = {"tags": MANAGED, "properties": {}}
    await vm(orphan).deprovision()  # failed earlier provision left only the NIC
    assert NIC not in orphan.resources

    start_stop_only = vm(FakeArm(), subnet_id=None)
    assert ComputeOperation.PROVISION not in start_stop_only.supported_operations()
    assert ComputeOperation.START in start_stop_only.supported_operations()
    locked = vm(FakeArm(), allow_lifecycle=False)
    with pytest.raises(LifecycleDisabled):
        await locked.provision()
