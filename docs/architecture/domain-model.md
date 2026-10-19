# Domain model

Source: `python/omf/core/domain/` (`models.py`, `capabilities.py`, `events.py`, `errors.py`).
Entities are pydantic models ([ADR-0002](adr/0002-pydantic-in-domain.md)).

## Entities

| Entity | Purpose | Key fields |
|---|---|---|
| `ModelDefinition` | What a model *is*, independent of where it runs | `id`, `family`, `revision`, `parameters`, `input_modalities`/`output_modalities`, `context_window`, `max_output_tokens`, `quantizations`, `runtime_compatibility`, `license` (`ModelLicense`), `artifacts` (`ArtifactRef`), `capabilities`, `provider_aliases`, `source` (`catalog`/`api`/`discovered`) |
| `ModelDeployment` | A model as served by one provider | `id` = `"{provider_id}/{provider_model}"`, `provider_model` (native name, e.g. `qwen2.5:0.5b`), `model_id` (catalog id, if mapped), `compute_environment_id`, `state`, `capabilities`, `context_window`, `quantization`, `family`, `details` |
| `DiscoveredModel` | What an adapter reports from `list_models()` | `name`, `capabilities`, `capabilities_complete` (engine list is exhaustive), `context_window`, `quantization` |
| `DeclaredDeployment` | Operator declaration in a provider's `models:` list | `name`, `model_id`, `capabilities`, `context_window`, `quantization` |
| `ProviderDefinition` | Configuration of one inference endpoint | `id` (slug), `adapter_type`, `enabled`, `origin` (`config`/`runtime`), `locality` (`local`/`external`, `None` = adapter default), `allow_sensitive` (`None` = only if local), `compute_environment_id`, `timeouts` (`connect_s`, `read_s`, `total_s`), `models`, `config` (adapter-specific; secrets only as `*_ref`) |
| `ProviderCapabilities` | What an *adapter* supports at the protocol level | `adapter_type`, `adapter_version`, `operations`, `modalities`, `streaming`, `health_check`, `authentication`, `model_capabilities_source` |
| `ProviderHealth` | Latest health of a provider | `status`, `latency_ms`, `checked_at`, `message`, `error_code`, `consecutive_failures` |
| `ComputeEnvironment` | Where things run | `kind` (`local`, `colab_notebook`), `ephemeral`, `managed` (`automatic`/`manual`), `state`, `resources` (`ComputeResources`: CPU, memory, `GpuInfo[]`, `accelerator`, `source` detected/declared), `operations`, `endpoints` (`EndpointInfo` with `authorized`, `reachable`), `notes` |
| `StorageArtifact` | A stored file (not a running model) | `storage_provider_id`, `key`, `size_bytes`, `sha256`, `version`, `content_type`, `created_at`, `metadata` |
| `StorageStatus` | Health of a storage provider | `status`, `message`, `details`, `intended_use` |
| `ChatMessage` | One message | `role` (`system`/`user`/`assistant`), `content` (string or `TextPart`/`ImagePart` list), `id`, `created_at`, `metadata` (provider, model, usage, timings, status) |
| `InferenceRequest` | Request as seen by an adapter (already routed) | `request_id`, `model` (native name), `messages`, `params` (`GenerationParams`: temperature, max_tokens, top_p, stop, seed, response_format) |
| `GenerationChunk` / `GenerationResult` | Adapter output, normalised | `delta`/`content`, `finish_reason`, `usage` (`TokenUsage` with `source` provider/estimated) |
| `InferenceResponse` | Non-streaming API result | `request_id`, `provider_id`, `model`, `model_id`, `message`, `usage`, `timings` (`total_ms`, `ttft_ms`, `tokens_per_second`), `route` (`RouteDecision` with attempts), `conversation_id` |
| `InferenceRecord` | Observability record (no prompt/completion text) | status, provider, model, attempts, latency, TTFT, tokens, tokens/s, error code |
| `Conversation` | Chat history | `id`, `title`, timestamps, ordered `messages` |
| `RoutingPolicy` | How a provider is chosen | `mode`, `default_provider_id`, `preferred_provider_ids`, `fallback_provider_ids`, `allow_cross_provider_fallback` (default false), `max_attempts`, `respect_health` |
| `BenchmarkRun` | Reproducible benchmark | `dataset_id` + `dataset_version`, `targets`, `params`, `warmup_runs`, `iterations`, `samples` (`BenchmarkSample`: phase `cold`/`warm`, TTFT, total, tokens, tokens/s, `token_count_source`, error), `summaries` (`BenchmarkSummary` with `TargetEnvironment`, `comparability_key`), `comparable`, `comparability_notes` |
| `AuditEntry` | Configuration/security action | `actor`, `action`, `target`, `request_id`, `details` |

## Lifecycle states

| Enum | Values |
|---|---|
| `HealthStatus` | `unknown` (never checked), `up`, `degraded`, `down`, `disabled` |
| `DeploymentState` | `available` (listed by provider), `unavailable` (declared but not served), `unknown` (provider unreachable) |
| `ComputeState` | `online`, `degraded`, `offline`, `unknown` |
| `RequestStatus` | `pending`, `streaming`, `completed`, `failed`, `cancelled` |
| `BenchmarkStatus` | `pending`, `running`, `completed`, `failed`, `cancelled` |
| `Locality` / `Sensitivity` | `local`/`external`; `normal`/`sensitive` |
| `RoutingMode` | `explicit`, `default`, `preferred_with_fallback`, `capability_based` |

## Capability evidence

Vocabulary (`Capability`): `text_generation`, `chat`, `vision_input`, `embeddings`,
`tool_calling`, `structured_output`, `streaming`, `audio_input`, `audio_output`. The audio
capabilities exist in the vocabulary only; no adapter implements audio requests.

Each `CapabilityClaim` carries one of three `Evidence` levels, from strongest to weakest:

| Evidence | Meaning | Produced by |
|---|---|---|
| `verified` | OMF exercised the capability successfully | `POST /api/v1/deployments/verify` (chat, text_generation, streaming, structured_output probes) |
| `provider_reported` | The engine reported it | Ollama `/api/show` `capabilities`, mock adapter |
| `declared` | Operator, catalog or adapter default; unverified | provider `models:` entries, catalog entries, OpenAI-compatible "listed model ⇒ chat", streaming for chat-capable deployments of streaming adapters |

`CapabilitySet` keeps only the strongest claim for each capability. A capability with no
claim is **unsupported**. When an engine reports a complete list
(`capabilities_complete`), declarations cannot add capabilities the engine leaves out.
See [ADR-0003](adr/0003-capability-evidence.md).

## Streaming events

Defined in `events.py`. Every stream ends with exactly one terminal event.

| Event | Data |
|---|---|
| `message.started` | `request_id`, `conversation_id`, `provider_id`, `model`, `model_id`, `route` |
| `message.delta` | `request_id`, `index`, `delta` |
| `usage.reported` | `request_id`, `usage` |
| `message.completed` | `request_id`, `message_id`, `conversation_id`, `finish_reason`, `timings`, `usage` |
| `request.failed` | `request_id`, `error` (`code`, `message`, `retryable`, `details`), `partial` (deltas already delivered) |

## Exceptions → HTTP status

All domain errors derive from `OMFError` (`code`, `message`, `details`, `retryable`).
`apps/api/omf_api/errors.py` maps them in this order. The first match wins, so a subclass
is listed before its base class.

| Error (`code`) | HTTP |
|---|---|
| `ContextLengthExceeded` (`context_length_exceeded`), `ValidationFailed` (`validation_failed`), `EgressDenied` (`egress_denied`), `CapabilityNotSupported` (`capability_not_supported`) | 422 |
| `NotFound` and subclasses (`provider_not_found`, `model_not_found`, `conversation_not_found`, `artifact_not_found`, `benchmark_not_found`, `compute_environment_not_found`, `storage_provider_not_found`) | 404 |
| `Conflict`, `ModelUnavailable`, `ChecksumMismatch`, `RequestCancelled` | 409 |
| `OperationNotSupported` (`operation_not_supported`) | 501 |
| `ProviderTimeout` | 504 |
| `ProviderUnavailable`, `ProviderDisabled`, `NoRouteAvailable` | 503 |
| `ProviderAuthenticationFailed`, `ProviderError`, `StorageError` | 502 |
| `PolicyViolation`, `PermissionDenied` | 403 |
| `AuthenticationRequired` | 401 (with `WWW-Authenticate: Bearer`) |
| `SecretNotFound`, `ConfigurationError`, unmapped `OMFError` | 500 |
| Request validation (FastAPI) | 422 `validation_failed` (input is not echoed) |
| Body too large | 413 `request_too_large` |
| Unhandled exception | 500 `internal_error` (generic message) |

Retryable by default: `ProviderUnavailable`, `ProviderTimeout`. `StorageError` and
`EgressDenied` are retryable when raised for transient causes (HTTP 429/5xx, DNS
resolution failure).
