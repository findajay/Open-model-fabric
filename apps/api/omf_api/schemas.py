"""API data-transfer objects. Every JSON success response is an :class:`Envelope`."""

from __future__ import annotations

from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import ConfigDict, Field

from omf.core.domain.base import DomainModel
from omf.core.domain.capabilities import Capability
from omf.core.domain.models import (
    SLUG_PATTERN,
    ChatMessage,
    ContentPart,
    DeclaredDeployment,
    GenerationParams,
    Locality,
    ModelDefinition,
    ModelDeployment,
    ProviderCapabilities,
    ProviderDefinition,
    ProviderHealth,
    ResponseFormat,
    Role,
    Sensitivity,
    Timeouts,
)
from omf.core.ports.secrets import SecretReferenceInfo
from omf_api.context import current_request_id

T = TypeVar("T")


class Meta(DomainModel):
    request_id: str | None = None


class Envelope(DomainModel, Generic[T]):
    data: T
    meta: Meta = Field(default_factory=Meta)


def ok(data: T) -> Envelope[T]:
    return Envelope[T](data=data, meta=Meta(request_id=current_request_id()))


# ---------------------------------------------------------------------- chat


class ChatMessageIn(DomainModel):
    model_config = ConfigDict(extra="forbid")

    role: Role
    content: (
        Annotated[str, Field(max_length=200_000)]
        | Annotated[list[ContentPart], Field(max_length=64)]
    )


class ChatRequest(DomainModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessageIn] = Field(min_length=1, max_length=200)
    provider_id: str | None = Field(
        default=None, description="Explicit provider; disables fallback"
    )
    model: str | None = Field(
        default=None, description="Provider-native model name or catalog model id"
    )
    conversation_id: str | None = None
    persist: bool = Field(default=False, description="Create a conversation if none is given")
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=131072)
    top_p: float | None = Field(default=None, gt=0, le=1)
    stop: list[str] | None = Field(default=None, max_length=8)
    seed: int | None = None
    response_format: ResponseFormat = ResponseFormat.TEXT
    sensitivity: Sensitivity = Sensitivity.NORMAL
    required_capabilities: list[Capability] = Field(default_factory=list)

    def params(self) -> GenerationParams:
        return GenerationParams(
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            top_p=self.top_p,
            stop=self.stop,
            seed=self.seed,
            response_format=self.response_format,
        )

    def domain_messages(self) -> list[ChatMessage]:
        return [ChatMessage(role=m.role, content=m.content) for m in self.messages]


class CancelResult(DomainModel):
    request_id: str
    cancelled: bool


# ---------------------------------------------------------------------- providers


class ProviderView(DomainModel):
    """A provider as exposed over the API. Contains secret *references* only."""

    id: str
    adapter_type: str
    display_name: str | None
    enabled: bool
    origin: Literal["config", "runtime"]
    locality: Locality
    allow_sensitive: bool
    compute_environment_id: str | None
    timeouts: Timeouts
    models: list[DeclaredDeployment]
    config: dict[str, Any]
    secret_references: list[SecretReferenceInfo]
    capabilities: ProviderCapabilities
    health: ProviderHealth
    is_default: bool


class ProviderCreate(DomainModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=SLUG_PATTERN)
    adapter_type: str
    display_name: str | None = None
    enabled: bool = True
    locality: Locality | None = None
    allow_sensitive: bool | None = None
    compute_environment_id: str | None = None
    timeouts: Timeouts = Field(default_factory=Timeouts)
    models: list[DeclaredDeployment] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)

    def to_domain(self) -> ProviderDefinition:
        return ProviderDefinition(**self.model_dump(), origin="runtime")


class ProviderUpdate(DomainModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = None
    enabled: bool | None = None
    locality: Locality | None = None
    allow_sensitive: bool | None = None
    compute_environment_id: str | None = None
    timeouts: Timeouts | None = None
    models: list[DeclaredDeployment] | None = None
    config: dict[str, Any] | None = None


class ProviderTestRequest(DomainModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str | None = Field(default=None, description="Test an existing provider")
    definition: ProviderCreate | None = Field(
        default=None, description="Or test an unsaved definition"
    )


class ProviderMutationResult(DomainModel):
    provider: ProviderView
    warnings: list[str]


# ---------------------------------------------------------------------- models


class ModelDetail(DomainModel):
    definition: ModelDefinition
    deployments: list[ModelDeployment]


class ModelCreate(DomainModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
    display_name: str
    family: str | None = None
    revision: str | None = None
    parameters: str | None = None
    description: str | None = None
    context_window: int | None = Field(default=None, gt=0)
    quantizations: list[str] = Field(default_factory=list)
    runtime_compatibility: list[str] = Field(default_factory=list)
    capabilities: list[Capability] = Field(default_factory=list)
    provider_aliases: list[str] = Field(default_factory=list)
    license: dict[str, Any] | None = None


class VerifyRequest(DomainModel):
    provider_id: str
    model: str


# ---------------------------------------------------------------------- conversations


class ConversationCreate(DomainModel):
    title: str = Field(default="New conversation", max_length=200)


class ConversationUpdate(DomainModel):
    title: str = Field(min_length=1, max_length=200)


# ---------------------------------------------------------------------- dashboard


class ProviderStats(DomainModel):
    provider_id: str
    requests: int
    errors: int
    error_rate: float
    p50_latency_ms: float | None
    p95_latency_ms: float | None
    mean_ttft_ms: float | None
    mean_tokens_per_second: float | None


class MetricsSummary(DomainModel):
    window_requests: int
    providers: list[ProviderStats]


class SystemInfo(DomainModel):
    name: str = "Open Model Fabric"
    version: str
    profile: str
    environment: str
    auth_mode: str
    principal: str
    role: str
    adapters: list[str]


class OperationResult(DomainModel):
    ok: bool
    message: str | None = None
