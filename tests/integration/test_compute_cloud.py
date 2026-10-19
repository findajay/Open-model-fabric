"""Kubernetes, EC2, Compute Engine and Azure compute adapters against fake APIs.

These are NOT tested against real clouds. Kubernetes, Compute Engine and Azure use
in-memory fakes of the REST endpoints the adapters call (``httpx.MockTransport``); EC2
uses moto. They verify request shapes, the ownership rules and state mapping.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from omf.adapters.compute.aws_ec2 import AwsEc2ComputeAdapter, AwsEc2ComputeConfig
from omf.adapters.compute.azure_vm import (
    AzureClientCredentials,
    AzureVmComputeAdapter,
    AzureVmComputeConfig,
)
from omf.adapters.compute.gcp_compute import GcpComputeAdapter, GcpComputeConfig
from omf.adapters.compute.kubernetes import (
    KubernetesComputeAdapter,
    KubernetesComputeConfig,
    parse_quantity,
)
from omf.core.domain.errors import ComputeError, Conflict, LifecycleDisabled, OperationNotSupported
from omf.core.domain.models import ComputeOperation, ComputeState
from omf.core.ports.secrets import SecretValue
from omf.core.security.egress import EgressPolicy

EGRESS = EgressPolicy()
MANAGED = {"omf-managed": "true", "omf-environment": "gpu"}


async def token() -> str:
    return "test-token"


# ---------------------------------------------------------------------- Kubernetes


class FakeKubernetes:
    """Just enough of the Kubernetes API for the adapter: deployments, scale, services, nodes."""

    def __init__(self, *, nodes_forbidden: bool = False) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.nodes_forbidden = nodes_forbidden
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["authorization"] == "Bearer k8s-token"
        path = request.url.path
        if path == "/api/v1/nodes":
            if self.nodes_forbidden:
                return httpx.Response(403, json={"message": "nodes is forbidden"})
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {
                                "labels": {
                                    "nvidia.com/gpu.product": "NVIDIA-L4",
                                    "nvidia.com/gpu.memory": "23034",
                                }
                            },
                            "status": {
                                "allocatable": {
                                    "cpu": "7910m",
                                    "memory": "30Gi",
                                    "nvidia.com/gpu": "1",
                                }
                            },
                        },
                        {
                            "metadata": {"labels": {}},
                            "status": {"allocatable": {"cpu": "4", "memory": "16Gi"}},
                        },
                    ]
                },
            )
        if request.method == "POST":
            body = json.loads(request.content)
            key = f"{path}/{body['metadata']['name']}"
            if key in self.objects:
                return httpx.Response(409, json={"message": "already exists"})
            self.objects[key] = body
            return httpx.Response(201, json=body)
        if path.endswith("/scale") and request.method == "PATCH":
            assert request.headers["content-type"] == "application/merge-patch+json"
            obj = self.objects[path.removesuffix("/scale")]
            replicas = json.loads(request.content)["spec"]["replicas"]
            obj["spec"]["replicas"] = replicas
            obj["status"] = {"readyReplicas": replicas}  # the fake becomes ready at once
            return httpx.Response(200, json={"spec": {"replicas": replicas}})
        if request.method == "GET":
            obj = self.objects.get(path)
            return httpx.Response(200, json=obj) if obj else httpx.Response(404, json={})
        if request.method == "DELETE":
            return (
                httpx.Response(200, json={})
                if self.objects.pop(path, None)
                else httpx.Response(404, json={})
            )
        return httpx.Response(405)


def k8s(fake: FakeKubernetes, **overrides: Any) -> KubernetesComputeAdapter:
    raw: dict[str, Any] = {
        "api_server": "https://k8s.example.com:6443",
        "namespace": "ml",
        "name": "vllm",
        "image": "vllm/vllm-openai:v0.32.0",
        "gpus": 1,
        "allow_lifecycle": True,
        "args": ["--model", "Qwen/Qwen2.5-0.5B-Instruct"],
        "env_from_secrets": ["hf-token"],
        "node_selector": {"nvidia.com/gpu.product": "NVIDIA-L4"},
    }
    raw.update(overrides)
    return KubernetesComputeAdapter(
        "gpu",
        KubernetesComputeConfig.model_validate(raw),
        EGRESS,
        token=SecretValue("k8s-token"),
        transport=httpx.MockTransport(fake.handler),
    )


DEPLOYMENT = "/apis/apps/v1/namespaces/ml/deployments/vllm"
SERVICE = "/api/v1/namespaces/ml/services/vllm"


async def test_kubernetes_lifecycle() -> None:
    fake = FakeKubernetes()
    adapter = k8s(fake)
    env = await adapter.describe_environment()
    assert env.state == ComputeState.OFFLINE and "Not provisioned." in env.notes
    assert env.resources is not None
    assert env.resources.cpu_count == 11 and env.resources.accelerator == "cuda"
    assert [g.name for g in env.resources.gpus] == ["NVIDIA-L4"]
    assert env.resources.gpus[0].memory_total_bytes == 23034 * 2**20

    env = await adapter.provision()
    deployment = fake.objects[DEPLOYMENT]
    assert deployment["spec"]["replicas"] == 0
    assert deployment["metadata"]["labels"]["omf-managed"] == "true"
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"]["limits"]["nvidia.com/gpu"] == "1"
    assert container["envFrom"] == [{"secretRef": {"name": "hf-token"}}]
    assert deployment["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
    assert fake.objects[SERVICE]["spec"]["selector"] == {"omf-environment": "gpu"}
    assert env.state == ComputeState.OFFLINE
    assert env.endpoints[0].url == "http://vllm.ml.svc.cluster.local:8000"
    await adapter.provision()  # idempotent: no 409 from a second POST

    env = await adapter.start()
    assert env.state == ComputeState.ONLINE and env.details["replicas_ready"] == 1
    env = await adapter.stop()
    assert env.state == ComputeState.OFFLINE
    env = await adapter.deprovision()
    assert fake.objects == {} and "Not provisioned." in env.notes
    await adapter.deprovision()  # idempotent
    await adapter.aclose()


async def test_kubernetes_refuses_foreign_objects() -> None:
    fake = FakeKubernetes()
    fake.objects[DEPLOYMENT] = {
        "metadata": {"name": "vllm", "labels": {"app": "theirs"}},
        "spec": {"replicas": 2},
        "status": {"readyReplicas": 2},
    }
    adapter = k8s(fake)
    for op in (adapter.provision, adapter.start, adapter.stop, adapter.deprovision):
        with pytest.raises(Conflict):
            await op()
    assert fake.objects[DEPLOYMENT]["spec"]["replicas"] == 2  # untouched
    assert not any(r.method in ("PATCH", "DELETE") for r in fake.requests)
    env = await adapter.describe_environment()
    assert any("not managed by Open Model Fabric" in n for n in env.notes)
    await adapter.aclose()


async def test_kubernetes_without_node_permission_and_without_provisioning() -> None:
    adapter = k8s(FakeKubernetes(nodes_forbidden=True))
    env = await adapter.describe_environment()
    assert env.resources is None
    assert any("Not permitted to list nodes" in n for n in env.notes)
    with pytest.raises(ComputeError, match="not provisioned"):
        await adapter.start()
    await adapter.aclose()


def test_kubernetes_config_and_quantities() -> None:
    assert parse_quantity("3500m") == 3.5 and parse_quantity("16Gi") == 16 * 2**30
    assert parse_quantity("2") == 2 and parse_quantity("1k") == 1000
    base = {"name": "vllm", "image": "x:1"}
    with pytest.raises(ValidationError):
        KubernetesComputeConfig.model_validate(base)  # no api_server, not in-cluster
    with pytest.raises(ValidationError):
        KubernetesComputeConfig.model_validate({**base, "api_server": "https://k", "name": "Bad_"})
    with pytest.raises(ValidationError):
        KubernetesComputeConfig.model_validate(
            {**base, "api_server": "https://k", "client_cert_ref": "env:C"}
        )


# ---------------------------------------------------------------------- EC2 (moto)


@pytest.fixture
def ec2() -> Iterator[Any]:
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")
    with moto.mock_aws():
        yield boto3.client("ec2", region_name="us-east-1")


def ec2_adapter(client: Any, **overrides: Any) -> AwsEc2ComputeAdapter:
    raw: dict[str, Any] = {
        "region": "us-east-1",
        "image_id": "ami-03cf127a",
        "instance_type": "g5.xlarge",
        "allow_lifecycle": True,
        "endpoint_port": 8000,
        "probe_endpoint": False,
        "user_data": "#!/bin/bash\necho start vllm",
    }
    raw.update(overrides)
    return AwsEc2ComputeAdapter(
        "gpu", AwsEc2ComputeConfig.model_validate(raw), EGRESS, client=client
    )


async def test_ec2_managed_instance_lifecycle(ec2: Any) -> None:
    adapter = ec2_adapter(ec2)
    env = await adapter.describe_environment()
    assert env.state == ComputeState.OFFLINE and "Not provisioned." in env.notes
    assert env.resources is not None and env.resources.cpu_count == 4
    assert [g.name for g in env.resources.gpus] == ["NVIDIA A10G"]
    assert env.resources.accelerator == "cuda"

    env = await adapter.provision()
    assert env.state == ComputeState.ONLINE  # moto launches straight into "running"
    instance_id = env.details["instance_id"]
    tags = {
        t["Key"]: t["Value"]
        for t in ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][
            0
        ]["Tags"]
    }
    assert tags["omf-managed"] == "true" and tags["omf-environment"] == "gpu"
    assert env.endpoints[0].url.endswith(":8000")
    await adapter.provision()  # idempotent: still one instance
    assert len(ec2.describe_instances()["Reservations"]) == 1

    assert (await adapter.stop()).state == ComputeState.OFFLINE
    assert (await adapter.start()).state == ComputeState.ONLINE
    env = await adapter.deprovision()
    assert "Not provisioned." in env.notes  # terminated instances are no longer matched


async def test_ec2_designated_instance_is_never_terminated(ec2: Any) -> None:
    instance_id = ec2.run_instances(
        ImageId="ami-03cf127a", InstanceType="t3.micro", MinCount=1, MaxCount=1
    )["Instances"][0]["InstanceId"]
    adapter = ec2_adapter(ec2, instance_id=instance_id, image_id=None, instance_type=None)
    env = await adapter.describe_environment()
    assert any("deprovision (terminate) refused" in n for n in env.notes)
    await adapter.provision()  # no-op for a designated instance
    assert (await adapter.stop()).state == ComputeState.OFFLINE
    with pytest.raises(Conflict):
        await adapter.deprovision()
    state = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    assert state["State"]["Name"] == "stopped"


async def test_ec2_errors_and_config(ec2: Any) -> None:
    adapter = ec2_adapter(ec2)
    with pytest.raises(ComputeError, match="no instance is provisioned"):
        await adapter.start()
    bad = ec2_adapter(ec2, instance_id="i-0123456789abcdef0", image_id=None, instance_type=None)
    with pytest.raises(ComputeError, match="EC2 describe_instances failed"):
        await bad.describe_environment()
    with pytest.raises(ValidationError):
        AwsEc2ComputeConfig.model_validate({"region": "us-east-1"})  # nothing to launch


# ---------------------------------------------------------------------- Compute Engine


class FakeComputeEngine:
    ZONE = "/compute/v1/projects/omf-test-project/zones/us-central1-a"

    def __init__(self, *, pending_operations: bool = False) -> None:
        self.instance: dict[str, Any] | None = None
        self.pending = pending_operations
        self.calls: list[tuple[str, str]] = []

    def op(self, name: str) -> httpx.Response:
        return httpx.Response(
            200, json={"name": name, "status": "RUNNING" if self.pending else "DONE"}
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-token"
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        instance_path = f"{self.ZONE}/instances/llm"
        if path == f"{self.ZONE}/machineTypes/g2-standard-4":
            return httpx.Response(200, json={"guestCpus": 4, "memoryMb": 16384})
        if path.startswith(f"{self.ZONE}/operations/"):
            return httpx.Response(200, json={"name": path.rsplit("/", 1)[-1], "status": "DONE"})
        if path == f"{self.ZONE}/instances" and method == "POST":
            body = json.loads(request.content)
            if body.get("guestAccelerators", [{}])[0].get("acceleratorCount") == 8:
                return httpx.Response(
                    200,
                    json={
                        "name": "op-bad",
                        "status": "DONE",
                        "error": {"errors": [{"message": "Quota 'NVIDIA_L4_GPUS' exceeded"}]},
                    },
                )
            self.instance = {
                **body,
                "status": "RUNNING",
                "machineType": f"https://x/{body['machineType']}",
                "networkInterfaces": [{"networkIP": "10.128.0.5"}],
            }
            return self.op("op-insert")
        if path == instance_path:
            if method == "GET":
                return (
                    httpx.Response(200, json=self.instance)
                    if self.instance
                    else httpx.Response(404, json={"error": {"message": "not found"}})
                )
            if method == "DELETE":
                self.instance = None
                return self.op("op-delete")
        if path in (f"{instance_path}/start", f"{instance_path}/stop") and self.instance:
            self.instance["status"] = "RUNNING" if path.endswith("start") else "TERMINATED"
            return self.op("op-" + path.rsplit("/", 1)[-1])
        return httpx.Response(404, json={"error": {"message": f"unexpected {method} {path}"}})


def gce(fake: FakeComputeEngine, **overrides: Any) -> GcpComputeAdapter:
    raw: dict[str, Any] = {
        "project": "omf-test-project",
        "zone": "us-central1-a",
        "instance_name": "llm",
        "accelerator_type": "nvidia-l4",
        "accelerator_count": 1,
        "allow_lifecycle": True,
        "endpoint_port": 8000,
        "probe_endpoint": False,
        "startup_script": "docker run ...",
        "operation_wait_s": 0,
    }
    raw.update(overrides)
    return GcpComputeAdapter(
        "gpu",
        GcpComputeConfig.model_validate(raw),
        EGRESS,
        token,
        transport=httpx.MockTransport(fake.handler),
    )


async def test_gce_lifecycle() -> None:
    fake = FakeComputeEngine()
    adapter = gce(fake)
    env = await adapter.describe_environment()
    assert env.state == ComputeState.OFFLINE and "Not provisioned." in env.notes
    assert env.resources is not None and env.resources.cpu_count == 4
    assert [g.name for g in env.resources.gpus] == ["nvidia-l4"]

    env = await adapter.provision()
    assert fake.instance is not None
    assert fake.instance["labels"] == MANAGED
    assert fake.instance["scheduling"]["onHostMaintenance"] == "TERMINATE"  # required for GPUs
    assert fake.instance["guestAccelerators"][0]["acceleratorType"].endswith("/nvidia-l4")
    assert fake.instance["metadata"]["items"][0]["key"] == "startup-script"
    assert env.state == ComputeState.ONLINE
    assert env.endpoints[0].url == "http://10.128.0.5:8000"
    await adapter.provision()  # idempotent
    assert sum(1 for m, p in fake.calls if m == "POST" and p.endswith("/instances")) == 1

    assert (await adapter.stop()).state == ComputeState.OFFLINE
    assert (await adapter.start()).state == ComputeState.ONLINE
    env = await adapter.deprovision()
    assert fake.instance is None and "Not provisioned." in env.notes
    await adapter.aclose()


async def test_gce_refuses_foreign_instance_and_reports_operation_errors() -> None:
    fake = FakeComputeEngine()
    fake.instance = {
        "name": "llm",
        "status": "RUNNING",
        "labels": {"team": "theirs"},
        "machineType": "zones/us-central1-a/machineTypes/g2-standard-4",
    }
    adapter = gce(fake)
    for op in (adapter.provision, adapter.start, adapter.stop, adapter.deprovision):
        with pytest.raises(Conflict):
            await op()
    assert fake.instance["status"] == "RUNNING"
    assert not any(m in ("POST", "DELETE") for m, _ in fake.calls)

    failing = gce(FakeComputeEngine(), accelerator_count=8)
    with pytest.raises(ComputeError, match="Quota 'NVIDIA_L4_GPUS' exceeded"):
        await failing.provision()


async def test_gce_polls_pending_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("omf.adapters.compute.gcp_compute.asyncio.sleep", no_sleep)
    fake = FakeComputeEngine(pending_operations=True)
    await gce(fake, operation_wait_s=60).provision()
    assert ("GET", f"{fake.ZONE}/operations/op-insert") in fake.calls


# ---------------------------------------------------------------------- Azure


SUB = "00000000-0000-0000-0000-000000000001"
VM_PATH = (
    f"/subscriptions/{SUB}/resourceGroups/rg-llm/providers/Microsoft.Compute/virtualMachines/llm-vm"
)


class FakeAzure:
    def __init__(self, *, exists: bool = True) -> None:
        self.power = "PowerState/deallocated" if exists else None
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        if path.endswith("/oauth2/v2.0/token"):
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            assert form["client_secret"] == "s3cret" and form["grant_type"] == "client_credentials"
            return httpx.Response(200, json={"access_token": "aad-token", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer aad-token"
        assert request.url.params["api-version"] == "2024-07-01"
        if path.endswith("/locations/eastus/vmSizes"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"name": "Standard_NC4as_T4_v3", "numberOfCores": 4, "memoryInMB": 28672}
                    ]
                },
            )
        if path == VM_PATH and method == "GET":
            if self.power is None:
                return httpx.Response(404, json={"error": {"message": "ResourceNotFound"}})
            return httpx.Response(
                200,
                json={
                    "location": "eastus",
                    "properties": {
                        "hardwareProfile": {"vmSize": "Standard_NC4as_T4_v3"},
                        "instanceView": {
                            "statuses": [
                                {"code": "ProvisioningState/succeeded"},
                                {"code": self.power},
                            ]
                        },
                    },
                },
            )
        if path == f"{VM_PATH}/start" and method == "POST":
            self.power = "PowerState/running"
            return httpx.Response(202)
        if path == f"{VM_PATH}/deallocate" and method == "POST":
            self.power = "PowerState/deallocated"
            return httpx.Response(202)
        return httpx.Response(404, json={"error": {"message": f"unexpected {method} {path}"}})


def azure(fake: FakeAzure, **overrides: Any) -> AzureVmComputeAdapter:
    raw: dict[str, Any] = {
        "subscription_id": SUB,
        "resource_group": "rg-llm",
        "vm_name": "llm-vm",
        "tenant_id": "tenant",
        "client_id": "app",
        "client_secret_ref": "env:AZ_SECRET",
        "allow_lifecycle": True,
        "endpoint_host": "10.0.0.4",
        "endpoint_port": 8000,
        "probe_endpoint": False,
    }
    raw.update(overrides)
    config = AzureVmComputeConfig.model_validate(raw)
    transport = httpx.MockTransport(fake.handler)
    credentials = AzureClientCredentials(config, SecretValue("s3cret"), transport=transport)
    return AzureVmComputeAdapter("gpu", config, EGRESS, credentials, transport=transport)


async def test_azure_start_and_deallocate() -> None:
    fake = FakeAzure()
    adapter = azure(fake)
    env = await adapter.describe_environment()
    assert env.state == ComputeState.OFFLINE
    assert env.resources is not None and env.resources.cpu_count == 4
    assert env.resources.accelerator == "cuda"
    assert ComputeOperation.PROVISION not in env.operations
    assert ComputeOperation.START in env.operations

    assert (await adapter.start()).state == ComputeState.ONLINE
    assert env.endpoints[0].url == "http://10.0.0.4:8000"
    assert (await adapter.stop()).state == ComputeState.OFFLINE
    assert ("POST", f"{VM_PATH}/deallocate") in fake.calls  # stop deallocates (no billing)
    assert sum(1 for _, p in fake.calls if p.endswith("/token")) == 1  # token cached
    with pytest.raises(OperationNotSupported):
        await adapter.provision()
    await adapter.aclose()


async def test_azure_missing_vm_and_failed_auth() -> None:
    adapter = azure(FakeAzure(exists=False))
    env = await adapter.describe_environment()
    assert "Not provisioned." in env.notes
    with pytest.raises(ComputeError, match="not found"):
        await adapter.start()

    def deny(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    config = AzureVmComputeConfig.model_validate(
        {
            "subscription_id": SUB,
            "resource_group": "rg",
            "vm_name": "vm",
            "tenant_id": "t",
            "client_id": "c",
            "client_secret_ref": "env:X",
            "allow_lifecycle": True,
        }
    )
    transport = httpx.MockTransport(deny)
    denied = AzureVmComputeAdapter(
        "gpu",
        config,
        EGRESS,
        AzureClientCredentials(config, SecretValue("wrong"), transport=transport),
        transport=transport,
    )
    with pytest.raises(ComputeError, match="Azure authentication failed"):
        await denied.describe_environment()


async def test_lifecycle_disabled_by_default_for_every_cloud_adapter() -> None:
    adapters = [
        k8s(FakeKubernetes(), allow_lifecycle=False),
        gce(FakeComputeEngine(), allow_lifecycle=False),
        azure(FakeAzure(), allow_lifecycle=False),
    ]
    for adapter in adapters:
        assert ComputeOperation.START not in adapter.supported_operations()
        with pytest.raises(LifecycleDisabled):
            await adapter.start()
        env = await adapter.describe_environment()
        assert any("allow_lifecycle" in n for n in env.notes)
