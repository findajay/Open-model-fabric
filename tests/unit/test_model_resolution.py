"""Resolving a requested model name to a provider deployment."""

from __future__ import annotations

from typing import Any

import pytest

from omf.core.domain.errors import ModelNotFound
from omf.core.domain.models import DeploymentState, ModelDeployment
from omf.core.services.model_registry import ModelRegistryService


class _Registry(ModelRegistryService):
    """Registry with a fixed deployment list instead of live discovery."""

    def __init__(self, *deployments: ModelDeployment) -> None:
        self._fixed = list(deployments)

    async def deployments(self, provider_id: str | None = None, **_: Any) -> list[ModelDeployment]:
        return self._fixed

    async def discovery_error(self, provider_id: str) -> None:
        return None


def dep(name: str, state: DeploymentState = DeploymentState.AVAILABLE) -> ModelDeployment:
    return ModelDeployment(provider_id="ollama", provider_model=name, state=state)


async def test_untagged_name_resolves_to_latest() -> None:
    registry = _Registry(dep("all-minilm:latest"), dep("qwen2.5:0.5b"))
    resolved = await registry.resolve("ollama", "all-minilm")
    assert resolved.provider_model == "all-minilm:latest"


async def test_explicit_tag_resolves_exactly() -> None:
    registry = _Registry(dep("all-minilm:latest"), dep("qwen2.5:0.5b"))
    assert (await registry.resolve("ollama", "qwen2.5:0.5b")).provider_model == "qwen2.5:0.5b"
    latest = await registry.resolve("ollama", "all-minilm:latest")
    assert latest.provider_model == "all-minilm:latest"


async def test_untagged_name_does_not_match_other_tags() -> None:
    registry = _Registry(dep("all-minilm:latest"), dep("qwen2.5:0.5b"))
    with pytest.raises(ModelNotFound) as exc:
        await registry.resolve("ollama", "qwen2.5")
    assert exc.value.details["available"] == ["all-minilm:latest", "qwen2.5:0.5b"]


async def test_explicit_tag_does_not_fall_back_to_latest() -> None:
    registry = _Registry(dep("qwen2.5:latest"))
    with pytest.raises(ModelNotFound):
        await registry.resolve("ollama", "qwen2.5:0.5b")


async def test_exact_untagged_name_is_preferred_when_available() -> None:
    registry = _Registry(dep("phi"), dep("phi:latest"))
    assert (await registry.resolve("ollama", "phi")).provider_model == "phi"


async def test_available_latest_beats_declared_but_unserved_untagged_name() -> None:
    # A profile declaring "all-minilm" while Ollama lists "all-minilm:latest".
    registry = _Registry(dep("all-minilm", DeploymentState.UNAVAILABLE), dep("all-minilm:latest"))
    resolved = await registry.resolve("ollama", "all-minilm")
    assert resolved.provider_model == "all-minilm:latest"
    assert resolved.state == DeploymentState.AVAILABLE
