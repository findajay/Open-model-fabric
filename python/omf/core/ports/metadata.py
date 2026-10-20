"""Metadata repository port.

Split into small, cohesive repositories (interface segregation) that a single adapter
(``SqlMetadataStore``) implements. Services depend only on the repository they use.
All methods are mandatory.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from omf.core.domain.models import (
    AuditEntry,
    BenchmarkRun,
    ChatMessage,
    Conversation,
    InferenceRecord,
    ModelDefinition,
    ProviderDefinition,
)


class ProviderConfigRepository(ABC):
    @abstractmethod
    async def list_providers(self) -> list[ProviderDefinition]: ...

    @abstractmethod
    async def get_provider(self, provider_id: str) -> ProviderDefinition | None: ...

    @abstractmethod
    async def upsert_provider(self, provider: ProviderDefinition) -> None: ...

    @abstractmethod
    async def delete_provider(self, provider_id: str) -> bool: ...


class ModelRegistrationRepository(ABC):
    @abstractmethod
    async def list_models(self) -> list[ModelDefinition]: ...

    @abstractmethod
    async def get_model(self, model_id: str) -> ModelDefinition | None: ...

    @abstractmethod
    async def upsert_model(self, model: ModelDefinition) -> None: ...

    @abstractmethod
    async def delete_model(self, model_id: str) -> bool: ...


class ConversationRepository(ABC):
    @abstractmethod
    async def create_conversation(self, conversation: Conversation) -> None: ...

    @abstractmethod
    async def get_conversation(
        self, conversation_id: str, *, include_messages: bool = True
    ) -> Conversation | None: ...

    @abstractmethod
    async def list_conversations(
        self, *, limit: int = 50, owner: str | None = None
    ) -> list[Conversation]:
        """Newest first; ``owner`` restricts to one principal's conversations."""

    @abstractmethod
    async def append_messages(self, conversation_id: str, messages: list[ChatMessage]) -> None: ...

    @abstractmethod
    async def rename_conversation(self, conversation_id: str, title: str) -> None: ...

    @abstractmethod
    async def delete_conversation(self, conversation_id: str) -> bool: ...


class BenchmarkRepository(ABC):
    @abstractmethod
    async def save_benchmark(self, run: BenchmarkRun) -> None: ...

    @abstractmethod
    async def get_benchmark(self, run_id: str) -> BenchmarkRun | None: ...

    @abstractmethod
    async def list_benchmarks(self, *, limit: int = 50) -> list[BenchmarkRun]: ...


class SettingsRepository(ABC):
    @abstractmethod
    async def get_setting(self, key: str) -> Any | None: ...

    @abstractmethod
    async def set_setting(self, key: str, value: Any) -> None: ...

    @abstractmethod
    async def all_settings(self) -> dict[str, Any]: ...


class AuditLogRepository(ABC):
    @abstractmethod
    async def append_audit(self, entry: AuditEntry) -> None: ...

    @abstractmethod
    async def list_audit(self, *, limit: int = 100) -> list[AuditEntry]: ...


class InferenceRecordRepository(ABC):
    @abstractmethod
    async def add_record(self, record: InferenceRecord) -> None: ...

    @abstractmethod
    async def recent_records(self, *, limit: int = 50) -> list[InferenceRecord]: ...


class RequestCoordinationRepository(ABC):
    """Shared in-flight registry, so a cancel can reach the replica serving a request."""

    @abstractmethod
    async def register_inflight(self, request_id: str, owner: str | None, replica: str) -> None: ...

    @abstractmethod
    async def unregister_inflight(self, request_id: str) -> None: ...

    @abstractmethod
    async def inflight_owner(self, request_id: str) -> tuple[bool, str | None]:
        """``(found, owner)`` for an in-flight request on any replica."""

    @abstractmethod
    async def list_inflight(self, owner: str | None = None) -> list[str]: ...

    @abstractmethod
    async def request_cancel(self, request_id: str) -> None: ...

    @abstractmethod
    async def take_cancels(self, request_ids: list[str]) -> list[str]:
        """Return (and consume) pending cancel requests among ``request_ids``."""

    @abstractmethod
    async def prune_inflight(self, older_than_s: float) -> int:
        """Forget registrations left behind by crashed replicas."""


class MetadataStore(
    ProviderConfigRepository,
    ModelRegistrationRepository,
    ConversationRepository,
    BenchmarkRepository,
    SettingsRepository,
    AuditLogRepository,
    InferenceRecordRepository,
    RequestCoordinationRepository,
    ABC,
):
    """Aggregate implemented by metadata adapters."""

    @abstractmethod
    async def initialize(self) -> None:
        """Apply migrations. Idempotent."""

    @abstractmethod
    async def ping(self) -> bool: ...

    @abstractmethod
    async def aclose(self) -> None: ...
