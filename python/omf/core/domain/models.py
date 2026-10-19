"""Domain entities.

Key distinctions the model enforces:

* :class:`ModelDefinition` describes a model *independently of where it runs* (family,
  license, context window, artifacts). :class:`ModelDeployment` binds a model to a
  concrete inference provider and the name that provider uses for it.
* :class:`ProviderDefinition` is the configuration of an inference endpoint;
  :class:`ComputeEnvironment` is the machine it runs on. An inference protocol (e.g.
  OpenAI-compatible HTTP) does not change when the server moves between Colab, Docker
  and Kubernetes, so the two are linked only by an optional id.
* :class:`StorageArtifact` is a stored file (weights, configs, benchmark exports);
  it is not a running model.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, computed_field, field_validator

from omf.core.domain.base import DomainModel
from omf.core.domain.capabilities import Capability, CapabilitySet, Modality


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


SLUG_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,62}$"
Slug = Annotated[str, Field(pattern=SLUG_PATTERN)]


# =====================================================================================
# Lifecycle states
# =====================================================================================


class HealthStatus(StrEnum):
    UNKNOWN = "unknown"  # never checked
    UP = "up"
    DEGRADED = "degraded"  # reachable but slow or partially failing
    DOWN = "down"
    DISABLED = "disabled"  # administratively disabled; not checked


class DeploymentState(StrEnum):
    AVAILABLE = "available"  # the provider listed it in its latest discovery
    UNAVAILABLE = "unavailable"  # declared in config but the provider no longer lists it
    UNKNOWN = "unknown"  # provider not reachable, state cannot be determined


class ComputeState(StrEnum):
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class RequestStatus(StrEnum):
    PENDING = "pending"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BenchmarkStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Locality(StrEnum):
    """Data boundary of a provider. Drives the sensitive-prompt routing policy."""

    LOCAL = "local"  # runs on infrastructure the operator controls
    EXTERNAL = "external"  # third-party service; prompts leave the trust boundary


class Sensitivity(StrEnum):
    NORMAL = "normal"
    SENSITIVE = "sensitive"


# =====================================================================================
# Models
# =====================================================================================


class ModelLicense(DomainModel):
    """Model-weight license metadata. Open weights are not the same as open source:
    ``commercial_use`` and ``notes`` capture restrictions that SPDX ids alone miss."""

    name: str
    spdx_id: str | None = None
    url: str | None = None
    commercial_use: Literal["allowed", "restricted", "unknown"] = "unknown"
    notes: str | None = None


class ArtifactRef(DomainModel):
    storage_provider_id: str
    key: str
    format: str | None = None  # safetensors, gguf, ...
    quantization: str | None = None
    sha256: str | None = None


class ModelDefinition(DomainModel):
    id: str = Field(min_length=1, max_length=200)
    display_name: str
    family: str | None = None
    revision: str | None = None
    parameters: str | None = None  # e.g. "0.5B"
    description: str | None = None
    input_modalities: list[Modality] = Field(default_factory=lambda: [Modality.TEXT])
    output_modalities: list[Modality] = Field(default_factory=lambda: [Modality.TEXT])
    context_window: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    quantizations: list[str] = Field(default_factory=list)
    runtime_compatibility: list[str] = Field(default_factory=list)
    license: ModelLicense | None = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    capabilities: CapabilitySet = Field(default_factory=CapabilitySet)
    provider_aliases: list[str] = Field(default_factory=list)
    """Provider-native names that serve this model (e.g. ``qwen2.5:0.5b`` in Ollama)."""
    source: Literal["catalog", "api", "discovered"] = "catalog"


class ModelDeployment(DomainModel):
    """A model as served by a specific provider."""

    provider_id: str
    provider_model: str  # the name the provider uses, e.g. "qwen2.5:0.5b"
    model_id: str | None = None  # ModelDefinition.id when the deployment is mapped
    compute_environment_id: str | None = None
    state: DeploymentState = DeploymentState.UNKNOWN
    capabilities: CapabilitySet = Field(default_factory=CapabilitySet)
    context_window: int | None = None
    quantization: str | None = None
    family: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    discovered_at: datetime | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def id(self) -> str:
        return f"{self.provider_id}/{self.provider_model}"


class DiscoveredModel(DomainModel):
    """What an inference adapter reports in ``list_models``."""

    name: str
    family: str | None = None
    context_window: int | None = None
    quantization: str | None = None
    capabilities: CapabilitySet = Field(default_factory=CapabilitySet)
    capabilities_complete: bool = False
    """True when the engine reports an exhaustive capability list (e.g. Ollama >= 0.6
    ``/api/show``). Declarations may then not add capabilities the engine denies."""
    details: dict[str, Any] = Field(default_factory=dict)


# =====================================================================================
# Providers
# =====================================================================================


class ProviderOperation(StrEnum):
    LIST_MODELS = "list_models"
    GET_CAPABILITIES = "get_capabilities"
    GENERATE = "generate"
    STREAM_GENERATE = "stream_generate"
    HEALTH_CHECK = "health_check"
    CANCEL = "cancel"  # optional


MANDATORY_INFERENCE_OPERATIONS = frozenset(
    {
        ProviderOperation.LIST_MODELS,
        ProviderOperation.GET_CAPABILITIES,
        ProviderOperation.GENERATE,
        ProviderOperation.STREAM_GENERATE,
        ProviderOperation.HEALTH_CHECK,
    }
)


class AuthRequirement(StrEnum):
    NONE = "none"
    OPTIONAL_API_KEY = "optional_api_key"
    API_KEY = "api_key"


class ProviderCapabilities(DomainModel):
    """What an *adapter* can do (protocol level). Model capabilities live on deployments."""

    adapter_type: str
    adapter_version: str
    operations: list[ProviderOperation]
    modalities: list[Modality]
    streaming: bool
    health_check: bool
    authentication: AuthRequirement
    model_capabilities_source: str
    """How model capabilities are learned: ``declared``, ``provider_metadata``, ..."""


class Timeouts(DomainModel):
    connect_s: float = Field(default=5.0, gt=0, le=120)
    read_s: float = Field(default=120.0, gt=0, le=3600)
    """Maximum gap between bytes; for streaming this bounds the wait for the next token."""
    total_s: float = Field(default=300.0, gt=0, le=7200)


class DeclaredDeployment(DomainModel):
    """Operator declaration of a model served by a provider (unverified until probed)."""

    name: str = Field(min_length=1, max_length=200)
    model_id: str | None = None
    capabilities: list[Capability] = Field(default_factory=list)
    context_window: int | None = Field(default=None, gt=0)
    quantization: str | None = None


class ProviderDefinition(DomainModel):
    id: Slug
    adapter_type: str
    display_name: str | None = None
    enabled: bool = True
    origin: Literal["config", "runtime"] = "config"
    locality: Locality | None = None
    """Data boundary. ``None`` means "use the adapter's fail-safe default"."""
    allow_sensitive: bool | None = None
    """Whether prompts marked sensitive may be sent here. ``None``: only if locality is local."""
    compute_environment_id: str | None = None
    timeouts: Timeouts = Field(default_factory=Timeouts)
    models: list[DeclaredDeployment] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    """Adapter-specific configuration. Secrets appear only as references (``*_ref``)."""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ProviderHealth(DomainModel):
    provider_id: str
    status: HealthStatus = HealthStatus.UNKNOWN
    latency_ms: float | None = None
    checked_at: datetime | None = None
    message: str | None = None
    error_code: str | None = None
    consecutive_failures: int = 0


# =====================================================================================
# Compute
# =====================================================================================


class GpuInfo(DomainModel):
    name: str
    memory_total_bytes: int | None = None
    memory_used_bytes: int | None = None
    driver_version: str | None = None


class ComputeResources(DomainModel):
    cpu_count: int | None = None
    cpu_model: str | None = None
    architecture: str | None = None
    memory_total_bytes: int | None = None
    memory_available_bytes: int | None = None
    gpus: list[GpuInfo] = Field(default_factory=list)
    accelerator: Literal["none", "cuda", "rocm", "mps", "tpu", "unknown"] = "unknown"
    source: str = "detected"  # detected | reported | declared


class ComputeOperation(StrEnum):
    DESCRIBE = "describe_environment"
    GET_RESOURCES = "get_resources"
    GET_STATUS = "get_status"
    PROVISION = "provision"  # optional
    START = "start"  # optional
    STOP = "stop"  # optional


class EndpointInfo(DomainModel):
    url: str
    authorized: bool
    """Operator attests that exposing/using this endpoint is permitted by the platform terms."""
    reachable: bool | None = None
    checked_at: datetime | None = None
    message: str | None = None


class ComputeEnvironment(DomainModel):
    id: str
    kind: str  # local, colab_notebook, docker, kubernetes, ...
    display_name: str
    ephemeral: bool = False
    managed: Literal["automatic", "manual"] = "automatic"
    state: ComputeState = ComputeState.UNKNOWN
    resources: ComputeResources | None = None
    operations: list[ComputeOperation] = Field(default_factory=list)
    endpoints: list[EndpointInfo] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    checked_at: datetime | None = None


# =====================================================================================
# Storage
# =====================================================================================


class StorageArtifact(DomainModel):
    storage_provider_id: str
    key: str
    size_bytes: int
    sha256: str
    version: str
    content_type: str = "application/octet-stream"
    created_at: datetime
    metadata: dict[str, str] = Field(default_factory=dict)


class StorageStatus(DomainModel):
    storage_provider_id: str
    adapter_type: str
    status: HealthStatus
    message: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    intended_use: list[str] = Field(default_factory=list)
    """e.g. ``["model_artifacts", "experiment_outputs"]``. Google Drive is not a serving store."""


# =====================================================================================
# Inference
# =====================================================================================


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class TextPart(DomainModel):
    type: Literal["text"] = "text"
    text: str = Field(max_length=200_000)


ALLOWED_IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")


class ImagePart(DomainModel):
    type: Literal["image"] = "image"
    media_type: str
    data_base64: str = Field(max_length=8_000_000)  # ~6 MB decoded

    @field_validator("media_type")
    @classmethod
    def _check_media_type(cls, v: str) -> str:
        if v not in ALLOWED_IMAGE_TYPES:
            raise ValueError(f"unsupported image media type {v!r}")
        return v


ContentPart = Annotated[TextPart | ImagePart, Field(discriminator="type")]


class ChatMessage(DomainModel):
    role: Role
    content: str | list[ContentPart]
    id: str = Field(default_factory=lambda: new_id("msg"))
    created_at: datetime = Field(default_factory=utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return "".join(p.text for p in self.content if isinstance(p, TextPart))

    def images(self) -> list[ImagePart]:
        if isinstance(self.content, str):
            return []
        return [p for p in self.content if isinstance(p, ImagePart)]


class ResponseFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


class GenerationParams(DomainModel):
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1, le=131_072)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    stop: list[str] | None = Field(default=None, max_length=8)
    seed: int | None = None
    response_format: ResponseFormat = ResponseFormat.TEXT


class InferenceRequest(DomainModel):
    """A request as seen by an inference adapter: already routed to one provider/model."""

    request_id: str
    model: str  # provider-native model name
    messages: list[ChatMessage]
    params: GenerationParams = Field(default_factory=GenerationParams)


class TokenUsage(DomainModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    source: Literal["provider", "estimated"] = "provider"


class GenerationChunk(DomainModel):
    """Adapter-level stream item. Adapters normalise engine formats into this shape."""

    delta: str = ""
    finish_reason: str | None = None
    usage: TokenUsage | None = None


class GenerationResult(DomainModel):
    content: str
    finish_reason: str | None = None
    usage: TokenUsage | None = None


class Timings(DomainModel):
    total_ms: float
    ttft_ms: float | None = None
    tokens_per_second: float | None = None


class RouteAttempt(DomainModel):
    provider_id: str
    model: str
    outcome: Literal["selected", "skipped", "failed", "succeeded"]
    reason: str | None = None
    error_code: str | None = None


class RouteDecision(DomainModel):
    policy: str
    provider_id: str
    model: str
    model_id: str | None = None
    attempts: list[RouteAttempt] = Field(default_factory=list)


class InferenceResponse(DomainModel):
    request_id: str
    provider_id: str
    model: str
    model_id: str | None = None
    message: ChatMessage
    finish_reason: str | None = None
    usage: TokenUsage | None = None
    timings: Timings
    route: RouteDecision
    conversation_id: str | None = None


class InferenceRecord(DomainModel):
    """Observability record for one request. Never contains prompt or completion text."""

    request_id: str
    provider_id: str | None
    model: str | None
    status: RequestStatus
    streaming: bool
    routing_policy: str | None = None
    attempts: int = 0
    latency_ms: float | None = None
    ttft_ms: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    tokens_per_second: float | None = None
    error_code: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


# =====================================================================================
# Conversations
# =====================================================================================


class Conversation(DomainModel):
    id: str = Field(default_factory=lambda: new_id("conv"))
    title: str = Field(default="New conversation", max_length=200)
    owner: str | None = None
    """Principal that created it; only the owner (or an admin) may read or extend it."""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    messages: list[ChatMessage] = Field(default_factory=list)


# =====================================================================================
# Routing
# =====================================================================================


class RoutingMode(StrEnum):
    EXPLICIT = "explicit"  # caller names the provider; never falls back
    DEFAULT = "default"  # use the configured default provider
    PREFERRED_WITH_FALLBACK = "preferred_with_fallback"
    CAPABILITY_BASED = "capability_based"


class RoutingPolicy(DomainModel):
    mode: RoutingMode = RoutingMode.DEFAULT
    default_provider_id: str | None = None
    preferred_provider_ids: list[str] = Field(default_factory=list)
    fallback_provider_ids: list[str] = Field(default_factory=list)
    allow_cross_provider_fallback: bool = False
    """Cross-provider fallback is off unless an operator turns it on explicitly."""
    max_attempts: int = Field(default=2, ge=1, le=5)
    respect_health: bool = True
    """Skip providers whose last health check reported DOWN (non-explicit modes only)."""


# =====================================================================================
# Benchmarks
# =====================================================================================


class BenchmarkTarget(DomainModel):
    provider_id: str
    model: str


class BenchmarkSample(DomainModel):
    provider_id: str
    model: str
    prompt_id: str
    iteration: int
    phase: Literal["cold", "warm"]
    ttft_ms: float | None = None
    total_ms: float | None = None
    output_tokens: int | None = None
    token_count_source: Literal["provider", "estimated"] | None = None
    tokens_per_second: float | None = None
    error_code: str | None = None
    error_message: str | None = None


class TargetEnvironment(DomainModel):
    """What must match for two benchmark results to be comparable."""

    provider_id: str
    adapter_type: str
    model: str
    model_revision: str | None = None
    quantization: str | None = None
    compute_environment_id: str | None = None
    hardware: ComputeResources | None = None
    hardware_fingerprint: str = "unknown"


class BenchmarkSummary(DomainModel):
    provider_id: str
    model: str
    environment: TargetEnvironment
    comparability_key: str
    samples: int
    errors: int
    error_rate: float
    cold_total_ms: float | None = None
    cold_ttft_ms: float | None = None
    warm_p50_total_ms: float | None = None
    warm_p95_total_ms: float | None = None
    warm_mean_ttft_ms: float | None = None
    warm_mean_tokens_per_second: float | None = None


class BenchmarkRun(DomainModel):
    id: str = Field(default_factory=lambda: new_id("bench"))
    name: str = Field(max_length=200)
    status: BenchmarkStatus = BenchmarkStatus.PENDING
    dataset_id: str
    dataset_version: str
    targets: list[BenchmarkTarget]
    params: GenerationParams
    warmup_runs: int = Field(default=1, ge=0, le=10)
    iterations: int = Field(default=3, ge=1, le=50)
    samples: list[BenchmarkSample] = Field(default_factory=list)
    summaries: list[BenchmarkSummary] = Field(default_factory=list)
    comparable: bool = True
    comparability_notes: list[str] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class AuditEntry(DomainModel):
    id: str = Field(default_factory=lambda: new_id("audit"))
    actor: str
    action: str
    target: str
    request_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
