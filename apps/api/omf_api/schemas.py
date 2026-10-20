"""API data-transfer objects. Every JSON success response is an :class:`Envelope`."""

from __future__ import annotations

from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import ConfigDict, Field, field_validator, model_validator

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
    ToolCall,
    ToolDefinition,
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
    ) = ""
    tool_calls: list[ToolCall] = Field(
        default_factory=list,
        max_length=64,
        description="Assistant messages: tool calls returned by an earlier response",
    )
    tool_call_id: str | None = Field(
        default=None, max_length=128, description="Tool messages: the call being answered"
    )

    @model_validator(mode="after")
    def _roles(self) -> ChatMessageIn:
        if self.tool_calls and self.role != Role.ASSISTANT:
            raise ValueError("only assistant messages can carry tool_calls")
        if self.role == Role.TOOL and not self.tool_call_id:
            raise ValueError("tool messages need tool_call_id")
        if self.tool_call_id and self.role != Role.TOOL:
            raise ValueError("only tool messages can carry tool_call_id")
        if self.role == Role.TOOL and not isinstance(self.content, str):
            raise ValueError("tool message content must be a string")
        return self


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
    tools: list[ToolDefinition] = Field(
        default_factory=list,
        max_length=64,
        description="Functions the model may ask the caller to run. OMF never runs them.",
    )
    tool_choice: str = Field(
        default="auto",
        description="'auto', 'none', 'required', or the name of one tool to force",
    )

    @model_validator(mode="after")
    def _tools(self) -> ChatRequest:
        names = [t.name for t in self.tools]
        if len(set(names)) != len(names):
            raise ValueError("tool names must be unique")
        if self.tool_choice not in ("auto", "none", "required", *names):
            raise ValueError(f"tool_choice {self.tool_choice!r} is not one of the tools")
        if self.tool_choice == "required" and not self.tools:
            raise ValueError("tool_choice 'required' needs tools")
        if self.conversation_id is None:
            # Within a self-contained request, every tool result must answer a call
            # that an earlier assistant message made.
            seen: set[str] = set()
            for m in self.messages:
                seen.update(c.id for c in m.tool_calls)
                if m.role == Role.TOOL and m.tool_call_id not in seen:
                    raise ValueError(
                        f"tool message answers unknown tool_call_id {m.tool_call_id!r}"
                    )
        return self

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
        return [
            ChatMessage(
                role=m.role,
                content=m.content,
                tool_calls=m.tool_calls,
                tool_call_id=m.tool_call_id,
            )
            for m in self.messages
        ]


class EmbeddingsRequest(DomainModel):
    model_config = ConfigDict(extra="forbid")

    input: str | list[str] = Field(
        description="Text or list of texts (up to 256) to embed; one vector per text"
    )
    provider_id: str | None = Field(
        default=None, description="Explicit provider; disables fallback"
    )
    model: str | None = Field(
        default=None,
        description="Provider-native model name or catalog model id; default: the first "
        "model that declares the embeddings capability",
    )
    dimensions: int | None = Field(
        default=None, ge=1, le=8192, description="Requested vector size (model-dependent)"
    )
    sensitivity: Sensitivity = Sensitivity.NORMAL

    @field_validator("input")
    @classmethod
    def _inputs(cls, v: str | list[str]) -> str | list[str]:
        items = [v] if isinstance(v, str) else v
        if not 1 <= len(items) <= 256:
            raise ValueError("input must contain between 1 and 256 texts")
        if any(not text.strip() for text in items):
            raise ValueError("input texts must not be empty")
        return v

    def inputs(self) -> list[str]:
        return [self.input] if isinstance(self.input, str) else list(self.input)


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
