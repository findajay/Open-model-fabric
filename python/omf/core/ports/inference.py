"""Inference provider port.

Mandatory operations: ``get_capabilities``, ``list_models``, ``generate``,
``stream_generate``, ``health_check``.

Optional operations: ``cancel``. An adapter that does not support server-side
cancellation inherits the default implementation, which raises
:class:`~omf.core.domain.errors.OperationNotSupported`. Client-side cancellation
(closing the stream) is always available: the application service cancels the task that
consumes ``stream_generate``, and adapters must release their connections when the
async iterator is closed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator

from omf.core.domain.errors import OperationNotSupported
from omf.core.domain.models import (
    DiscoveredModel,
    GenerationChunk,
    GenerationResult,
    InferenceRequest,
    ProviderCapabilities,
    ProviderHealth,
)


class InferenceProvider(ABC):
    """An inference engine reachable through one protocol (mock, OpenAI-compatible, Ollama…)."""

    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id

    @abstractmethod
    def get_capabilities(self) -> ProviderCapabilities:
        """Static, protocol-level capabilities of this adapter instance."""

    @abstractmethod
    async def list_models(self) -> list[DiscoveredModel]:
        """Models the engine currently serves, with any capability metadata it reports."""

    @abstractmethod
    async def generate(self, request: InferenceRequest) -> GenerationResult:
        """Run a non-streaming chat completion."""

    @abstractmethod
    def stream_generate(self, request: InferenceRequest) -> AsyncGenerator[GenerationChunk, None]:
        """Run a streaming chat completion. Must be an async generator.

        Implementations normalise their engine's wire format into
        :class:`GenerationChunk` objects and translate failures into ``OMFError``.
        The final chunk carries ``finish_reason`` and, when the engine reports it,
        ``usage``.
        """

    @abstractmethod
    async def health_check(self) -> ProviderHealth:
        """Cheap liveness probe. Must not raise; failures are reported in the result."""

    async def cancel(self, request_id: str) -> None:
        """Optional: server-side cancellation of an in-flight request."""
        raise OperationNotSupported(
            f"provider {self.provider_id!r} does not support server-side cancellation",
            details={"operation": "cancel", "provider_id": self.provider_id},
        )

    async def aclose(self) -> None:
        """Release network clients. Called when the provider is reconfigured or removed."""
        return None
