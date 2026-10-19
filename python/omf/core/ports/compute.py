"""Compute provider port.

Mandatory: ``describe_environment``, ``get_resources``, ``get_status``.
Optional: ``provision``, ``start``, ``stop`` — raise ``OperationNotSupported`` unless
the adapter has an authorised mechanism to perform them. For example the Colab adapter
represents a *manually managed* notebook and therefore implements none of them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from omf.core.domain.errors import OperationNotSupported
from omf.core.domain.models import (
    ComputeEnvironment,
    ComputeOperation,
    ComputeResources,
    ComputeState,
)


class ComputeProvider(ABC):
    def __init__(self, environment_id: str) -> None:
        self.environment_id = environment_id

    @abstractmethod
    def supported_operations(self) -> list[ComputeOperation]: ...

    @abstractmethod
    async def describe_environment(self) -> ComputeEnvironment: ...

    @abstractmethod
    async def get_resources(self) -> ComputeResources | None: ...

    @abstractmethod
    async def get_status(self) -> ComputeState: ...

    def _unsupported(self, op: ComputeOperation) -> OperationNotSupported:
        return OperationNotSupported(
            f"compute environment {self.environment_id!r} does not support {op.value}",
            details={"operation": op.value, "environment_id": self.environment_id},
        )

    async def provision(self) -> ComputeEnvironment:
        raise self._unsupported(ComputeOperation.PROVISION)

    async def start(self) -> ComputeEnvironment:
        raise self._unsupported(ComputeOperation.START)

    async def stop(self) -> ComputeEnvironment:
        raise self._unsupported(ComputeOperation.STOP)

    async def aclose(self) -> None:
        return None
