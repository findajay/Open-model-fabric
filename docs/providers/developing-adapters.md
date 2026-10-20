# Developing adapters

Adapters implement the ports in `python/omf/core/ports` (see
[interfaces.md](../architecture/interfaces.md)). Adding one never requires changes to
`omf.core`; `tests/unit/test_architecture.py` enforces that.

## Inference adapter checklist

1. **Check whether you need one at all.** If the engine speaks `/v1/chat/completions`,
   configure `openai_compatible` instead ([ADR-0006](../architecture/adr/0006-openai-compatible-adapter-covers-many-engines.md)).
2. **Create `python/omf/adapters/inference/<name>.py`** with:
   - a config model with `model_config = ConfigDict(extra="forbid")`. URLs are plain
     fields. Credentials are `SecretRefStr` fields whose names end in `_ref`, never raw
     values.
   - a subclass of `InferenceProvider`. Implement `get_capabilities`, `list_models`,
     `generate`, `stream_generate` (an **async generator**), and `health_check` (must not
     raise). Override `cancel` only if the engine supports server-side cancellation.
     Override `aclose` to close clients.
   - a module-level `CAPABILITIES = ProviderCapabilities(...)` and a
     `create(definition, config, ctx)` factory.
3. **Translate every failure into `OMFError`.** For HTTP engines, reuse
   `adapters/inference/_http.py::HttpEndpoint`. It handles timeouts, the no-redirect
   policy, the egress re-check before connecting (`ensure_egress`), and status-to-error
   mapping (401/403 → auth failed, 404 → model not found, 408/429/5xx-transient →
   unavailable, others → provider error).
4. **Normalise streaming** at the adapter boundary. Yield `GenerationChunk(delta=...)`,
   then one final chunk with `finish_reason` and `usage` (when the engine reports it).
   Use `async with client.stream(...)` so that closing the generator closes the
   connection.
5. **Report capabilities honestly.**
   - Use `PROVIDER_REPORTED` only for what the engine reports.
   - Set `capabilities_complete=True` only if the engine's list is exhaustive.
   - Anything inferred from the protocol must be `DECLARED`, with a `source` such as
     `adapter:<type>`.
6. **Register it** in `python/omf/bootstrap/catalog.py`:

   ```python
   InferenceAdapterDescriptor(
       adapter_type=myengine.ADAPTER_TYPE,
       display_name="My Engine",
       version=myengine.ADAPTER_VERSION,
       description="...",
       config_model=myengine.MyEngineConfig,
       capabilities=myengine.CAPABILITIES,
       factory=myengine.create,
       default_locality=Locality.EXTERNAL,  # fail safe unless it is always self-hosted
       endpoint_fields=("base_url",),       # egress-checked
       secret_fields=("api_key_ref",),      # listed as references, resolved on the backend
   )
   ```

7. **Test it** independently:
   - Write a protocol emulator, or extend `tests/emulators/*`. Run it with the
     `ServerThread` fixture (`tests/conftest.py`) so the adapter talks to real sockets.
   - Cover streaming, the final usage chunk, unknown model → `ModelNotFound`, bad
     credentials → `ProviderAuthenticationFailed`, outage → `health_check` reports
     `down`, mid-stream errors, and that closing the stream aborts upstream generation.
     Follow `tests/integration/test_adapters.py`.
   - Add a provider-switching case to `tests/integration/test_api.py` to show the same
     client request works.

## Compute adapters

Subclass `ComputeProvider`. Implement `supported_operations`, `describe_environment`,
`get_resources` and `get_status`. Implement `provision`, `start`, `stop` and
`deprovision` only when an **authorised, supported** mechanism exists. Otherwise inherit
the defaults, which raise `OperationNotSupported` (501). Mark resources
`source="declared"` when they are not detected. Register a factory in `COMPUTE_ADAPTERS`
and the config model in `COMPUTE_CONFIG_MODELS` (both in `bootstrap/catalog.py`).

Adapters that change infrastructure subclass `ManagedComputeAdapter`
(`adapters/compute/_managed.py`) and follow [ADR-0012](../architecture/adr/0012-managed-compute-lifecycle.md).
The base class provides the `allow_lifecycle` opt-in, the per-environment lock,
endpoint probing through the egress policy, and `wait_for`. A subclass implements:

- `_inspect()`: read-only state, resources and endpoint host;
- the `_do_*` methods it supports, listed in `implemented`.

Label everything you create with `OWNER_LABEL`/`ENVIRONMENT_LABEL`, and raise `Conflict`
before you modify a resource that does not carry them (`owned()`). Map backend failures
to `ComputeError` (502). `_rest.RestClient` does this for JSON/REST APIs.

## Storage adapters

Subclass `ArtifactStorageProvider`. Requirements:

- validate every key with `validate_key` / `validate_prefix`;
- stream content: never read whole files into memory (`_common.spool_and_hash`,
  `HashingWriter`);
- record a SHA-256 and a version per `put`;
- raise `ChecksumMismatch` from `get` on corruption;
- raise `ArtifactNotFound` for missing keys.

Register a factory in `STORAGE_ADAPTERS`.

The adapter **must pass the shared contract suite**. Add a branch to the `backend`
fixture in `tests/contract/test_storage_contract.py`:

```python
@pytest.fixture(params=["local_fs", "google_drive", "s3", "my_store"])
async def backend(request, tmp_path):
    ...
    elif kind == "my_store":
        adapter = MyStoreAdapter("mine", MyStoreConfig(...))   # against a fake/emulator
        def corrupt(key: str) -> None:
            ...  # tamper with the stored bytes so the checksum tests can detect it
        yield Backend(adapter, corrupt)
```

The suite covers round trips, checksums, versioning, prefix listing, delete semantics,
corruption detection, unsafe keys, files of more than 2 MB, empty files and status.

## Do not

- Load adapters from configuration-supplied module paths, or run user-supplied code
  ([ADR-0004](../architecture/adr/0004-explicit-adapter-catalog-no-dynamic-loading.md)).
- Assume capabilities a model has not declared or reported
  ([ADR-0003](../architecture/adr/0003-capability-evidence.md)).
- Log or return secret values, prompts or completions.
- Follow redirects or skip the egress check for operator-supplied URLs.
- Retry non-idempotent generation inside the adapter. Retry and fallback decisions
  belong to the inference service.

## Future compatibility

| Target | Path | Status |
|---|---|---|
| vLLM | `openai_compatible` (`max_model_len` read as context window) | Supported by protocol; tested against the emulator, not a live vLLM |
| Hugging Face TGI (Messages API) | `openai_compatible` with `base_url: http://tgi/v1` | Supported by protocol; untested against live TGI |
| llama.cpp `llama-server` | `openai_compatible` | Supported by protocol; untested live |
| Hugging Face Inference Endpoints | `openai_compatible` (when the endpoint exposes the OpenAI route) with `api_key_ref` | Untested live |
| Custom REST model servers | `openai_compatible` if they implement the protocol; otherwise write a dedicated adapter as above | — |
| Docker (compute) | `docker` adapter: Engine API over a Unix socket or TCP/TLS; pinned images, named volumes only, GPUs via device requests | Implemented; tested against a real Docker Engine |
| Kubernetes (compute) | `kubernetes` adapter: an OMF-labelled Deployment + ClusterIP Service, scaled between 0 and `replicas` | Implemented; tested against a fake API server, not a real cluster |
| AWS EC2 (compute) | `aws_ec2` adapter: launches a tagged instance, or starts/stops a designated one | Implemented; tested against moto, not AWS |
| Google Compute Engine (compute) | `gcp_compute` adapter: labelled VM with optional GPUs and startup script | Implemented; tested against a fake REST API, not Google Cloud |
| Azure VMs (compute) | `azure_vm` adapter: provision (NIC + VM in an existing subnet), start, deallocate, delete; or start/stop a designated VM | Implemented; tested against a fake REST API, not Azure |
| AWS ECS (compute) | `aws_ecs` adapter: tagged task definition + service, desired count 0..N | Implemented; tested against moto, not AWS |
| Google Cloud Run (compute) | `gcp_cloud_run` adapter: labelled v2 service, min instances 0..N (stop = scale to zero), optional L4 GPU | Implemented; tested against a fake REST API, not Google Cloud |
| Azure Container Apps (compute) | `azure_container_apps` adapter: tagged app in an existing environment, native start/stop | Implemented; tested against a fake REST API, not Azure |
| S3, MinIO (storage) | `s3` adapter targets S3 and S3-compatible stores such as MinIO (`endpoint_url`, `force_path_style`). Contract-tested against moto, and against a real MinIO in the `Storage backends` CI workflow; not against AWS. | Implemented; MinIO tested in CI |
| Azure Blob (storage) | `azure_blob` adapter (REST, no SDK): Shared Key, SAS or Entra ID service principal; block uploads for large files | Implemented; contract-tested against Azurite (local and CI), not a real Azure account |
