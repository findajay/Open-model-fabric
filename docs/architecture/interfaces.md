# Interface contracts (ports)

Source: `python/omf/core/ports/`. All operations are `async` unless marked otherwise.
Optional operations have a default implementation that raises
`OperationNotSupported` (`operation_not_supported`, HTTP **501**). Adapters never
pretend to support something they can't do.

## InferenceProvider (`ports/inference.py`)

| Operation | Required | Contract |
|---|---|---|
| `get_capabilities()` (sync) | mandatory | Static, protocol-level `ProviderCapabilities` |
| `list_models()` | mandatory | `list[DiscoveredModel]`, with any capability metadata the engine reports |
| `generate(request)` | mandatory | Non-streaming completion → `GenerationResult` |
| `stream_generate(request)` | mandatory | **Async generator** of `GenerationChunk`. The final chunk carries `finish_reason` and, if known, `usage`. Engine formats are normalised here. Connections must be released when the generator is closed. |
| `health_check()` | mandatory | Must not raise; failures are reported in `ProviderHealth` |
| `cancel(request_id)` | optional | Server-side cancellation. Only the mock adapter implements it. |
| `aclose()` | lifecycle | Close network clients (called on reconfiguration or shutdown) |

Errors crossing this port must be `OMFError` subclasses. HTTP adapters translate them in
`adapters/inference/_http.py`:

| Upstream condition | Error |
|---|---|
| Connect, read or write network errors | `ProviderUnavailable` |
| Timeouts | `ProviderTimeout` |
| HTTP 401/403 | `ProviderAuthenticationFailed` |
| HTTP 404 | `ModelNotFound` |
| HTTP 408/429/502/503/504 | `ProviderUnavailable` |
| Other HTTP ≥ 400 | `ProviderError` (upstream message truncated to 300 chars) |

Client-side cancellation always works: the service cancels the task that consumes
`stream_generate`.

## ComputeProvider (`ports/compute.py`)

| Operation | Required |
|---|---|
| `supported_operations()` (sync), `describe_environment()`, `get_resources()`, `get_status()` | mandatory |
| `provision()`, `start()`, `stop()` | optional (raise `OperationNotSupported`) |

Neither built-in adapter (`local`, `colab_notebook`) implements lifecycle operations.
`POST /api/v1/compute/{id}/{provision|start|stop}` therefore returns 501.

## ArtifactStorageProvider (`ports/storage.py`)

Every operation is mandatory, and every adapter must pass
`tests/contract/test_storage_contract.py`.

| Operation | Contract |
|---|---|
| `put(key, source, *, content_type, metadata)` | Stream `source` to EOF and create a new version. Returns `StorageArtifact` with SHA-256. |
| `get(key, destination)` | Write the latest version to `destination`. Raises `ChecksumMismatch` if the content does not hash to the recorded SHA-256. |
| `list(prefix="")` | Latest version of each key under the prefix, sorted by key |
| `delete(key)` | Removes the key. Raises `ArtifactNotFound` if it is missing. |
| `get_metadata(key)` | `StorageArtifact` |
| `verify_checksum(key, expected_sha256=None)` | Re-hashes the stored bytes |
| `status()` | `StorageStatus` |

Keys are validated by `validate_key()`: relative, `/`-separated, segments matching
`[A-Za-z0-9._+=@-]{1,255}`, with no `.`, `..` or empty segment, and at most 1024
characters. User metadata is limited to 32 entries, with values of at most 256
characters.

## MetadataStore (`ports/metadata.py`)

The port is split into small repositories (interface segregation). One adapter,
`SqlMetadataStore`, implements all of them.

| Repository | Operations |
|---|---|
| `ProviderConfigRepository` | `list_providers`, `get_provider`, `upsert_provider`, `delete_provider` |
| `ModelRegistrationRepository` | `list_models`, `get_model`, `upsert_model`, `delete_model` |
| `ConversationRepository` | `create_conversation`, `get_conversation`, `list_conversations`, `append_messages`, `rename_conversation`, `delete_conversation` |
| `BenchmarkRepository` | `save_benchmark`, `get_benchmark`, `list_benchmarks` |
| `SettingsRepository` | `get_setting`, `set_setting`, `all_settings` |
| `AuditLogRepository` | `append_audit`, `list_audit` |
| `InferenceRecordRepository` | `add_record`, `recent_records` |
| `MetadataStore` (aggregate) | all of the above plus `initialize` (migrations, idempotent), `ping`, `aclose` |

## SecretProvider (`ports/secrets.py`)

| Operation | Contract |
|---|---|
| `resolve_secret(ref)` (sync) | Returns a `SecretValue` or raises `SecretNotFound`. `SecretValue.__repr__`/`__str__` print `**********`; only `.reveal()` returns the value. |
| `list_secret_references()` (sync) | References and their availability; never values |

References look like `env:NAME` or `file:/run/secrets/name` (`SecretRef`, `SecretRefStr`).
`SecretResolver` (core service) dispatches on the scheme.

## TelemetryProvider (`ports/telemetry.py`)

| Operation | Contract |
|---|---|
| `record_metric(name, value, *, unit, kind, attributes)` | `kind`: `counter`, `histogram`, `gauge` |
| `record_event(name, attributes)` | |
| `record_trace(name, *, duration_ms, status, attributes)` | A completed span |
| `span(name, attributes)` | Context-manager helper built on `record_trace` |
| `shutdown()` | Optional (no-op by default) |

Attributes never carry prompt or completion text.

## Adapter descriptor and catalog (`ports/registry.py`)

An inference adapter type declares itself with `InferenceAdapterDescriptor`:

| Field | Meaning |
|---|---|
| `adapter_type`, `display_name`, `version`, `description` | Identity |
| `config_model` | Pydantic model (use `extra="forbid"`); `describe()` publishes its JSON Schema |
| `capabilities` | Static `ProviderCapabilities` (operations, modalities, streaming, health check, auth requirement) |
| `factory(definition, config, ctx)` | Builds the adapter. `AdapterContext` supplies `resolve_secret`, `egress` (EgressPolicy) and `telemetry`. |
| `default_locality` | Fail-safe data boundary when the provider does not set one (`openai_compatible` defaults to `external`) |
| `endpoint_fields` | Config fields holding URLs; checked against the egress policy on save and at connect time |
| `secret_fields` | Config fields holding secret references |
| `future_compatible_with` | Informational |

`AdapterCatalog` is an explicit list assembled in `python/omf/bootstrap/catalog.py`.
Unknown `adapter_type` values give `validation_failed` with the list of available types.
Compute and storage adapter types are registered in the same module
(`COMPUTE_ADAPTERS`, `STORAGE_ADAPTERS`). `GET /api/v1/adapters` returns every descriptor
with its configuration schema. See [ADR-0004](adr/0004-explicit-adapter-catalog-no-dynamic-loading.md).
