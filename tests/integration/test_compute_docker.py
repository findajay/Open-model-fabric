"""Docker compute adapter against a REAL Docker Engine (skipped when no daemon is reachable)."""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Iterator

import pytest
from pydantic import ValidationError

from omf.adapters.compute.docker import DockerComputeAdapter, DockerComputeConfig
from omf.core.domain.errors import ComputeError, Conflict, LifecycleDisabled
from omf.core.domain.models import ComputeOperation, ComputeState
from omf.core.security.egress import EgressPolicy

SOCKET = os.environ.get("OMF_TEST_DOCKER_SOCKET", "/var/run/docker.sock")
IMAGE = "busybox:1.37.0"
pytestmark = pytest.mark.skipif(not os.path.exists(SOCKET), reason="no Docker daemon socket")
EGRESS = EgressPolicy(allow_loopback=True)


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False).stdout


@pytest.fixture
def name() -> Iterator[str]:
    container = f"omf-test-{uuid.uuid4().hex[:8]}"
    yield container
    docker("rm", "-f", container)


def config(name: str, **overrides: object) -> DockerComputeConfig:
    base: dict[str, object] = {
        "docker_host": f"unix://{SOCKET}",
        "container_name": name,
        "image": IMAGE,
        "allow_lifecycle": True,
        "network_mode": "host",
        "container_port": 18556,
        "command": ["httpd", "-f", "-p", "18556"],
        "env": {"MODE": "test"},
    }
    base.update(overrides)
    return DockerComputeConfig.model_validate(base)


async def test_full_lifecycle_against_real_docker(name: str) -> None:
    adapter = DockerComputeAdapter("docker-test", config(name), EGRESS)
    env = await adapter.describe_environment()
    assert env.state == ComputeState.OFFLINE and "Not provisioned." in env.notes
    assert ComputeOperation.START in env.operations
    assert env.resources is not None and env.resources.cpu_count

    env = await adapter.provision()
    assert env.state == ComputeState.OFFLINE  # created, not started
    labels = docker("inspect", "-f", "{{json .Config.Labels}}", name)
    assert '"omf-environment":"docker-test"' in labels and '"omf-managed":"true"' in labels
    await adapter.provision()  # idempotent

    env = await adapter.start()
    state = await adapter.wait_for({ComputeState.ONLINE}, timeout_s=30, interval_s=0.5)
    assert state == ComputeState.ONLINE
    env = await adapter.describe_environment()
    assert env.endpoints[0].url == "http://127.0.0.1:18556"
    assert env.endpoints[0].reachable is True  # the server in the container answered

    env = await adapter.stop()
    assert env.state == ComputeState.OFFLINE
    env = await adapter.deprovision()
    assert "Not provisioned." in env.notes
    assert docker("ps", "-aq", "--filter", f"name=^{name}$") == ""
    await adapter.aclose()


async def test_lifecycle_requires_opt_in(name: str) -> None:
    adapter = DockerComputeAdapter("docker-test", config(name, allow_lifecycle=False), EGRESS)
    with pytest.raises(LifecycleDisabled):
        await adapter.provision()
    assert ComputeOperation.PROVISION not in adapter.supported_operations()
    assert docker("ps", "-aq", "--filter", f"name=^{name}$") == ""
    await adapter.aclose()


async def test_never_touches_containers_it_does_not_own(name: str) -> None:
    docker("create", "--name", name, IMAGE, "sleep", "1")
    adapter = DockerComputeAdapter("docker-test", config(name), EGRESS)
    for op in (adapter.provision, adapter.start, adapter.stop, adapter.deprovision):
        with pytest.raises(Conflict):
            await op()
    env = await adapter.describe_environment()
    assert any("not managed by Open Model Fabric" in n for n in env.notes)
    assert docker("ps", "-aq", "--filter", f"name=^{name}$") != ""  # still there
    await adapter.aclose()


async def test_pull_failure_is_reported_and_nothing_is_created(name: str) -> None:
    adapter = DockerComputeAdapter(
        "docker-test", config(name, image="busybox:0.0.0-does-not-exist"), EGRESS
    )
    with pytest.raises(ComputeError):
        await adapter.provision()
    assert docker("ps", "-aq", "--filter", f"name=^{name}$") == ""
    await adapter.aclose()


def test_config_rejects_unpinned_images_and_host_mounts() -> None:
    for bad in (
        {"image": "ollama/ollama"},
        {"image": "ollama/ollama:latest"},
        {"volumes": ["/etc:/host-etc"]},
        {"container_name": "../x"},
        {"docker_host": "ssh://host"},
    ):
        with pytest.raises(ValidationError):
            config("ok-name", **bad)
    assert config("ok-name", volumes=["models:/root/.ollama"]).volumes == ["models:/root/.ollama"]
    bridge = config("ok-name", network_mode="bridge", container_port=11434)
    assert bridge.host_port == 11434 and bridge.endpoint_port == 11434
