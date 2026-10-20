# Implementation status

This page records what each phase delivered, how it was tested, and what is still
missing. It describes the state of the repository when it was written. When this page
and the code disagree, the code and its tests are correct.

**How things were verified.** Every claim below is backed by an automated test in the
repository unless it is marked otherwise. The development sandbox could not reach
Ollama's model registry, Hugging Face, Google Drive, AWS, Google Cloud or Azure, and had
no GPU. A Docker daemon was available only later, for the compute adapters. So:

* real inference engines were replaced by **protocol emulators** (`tests/emulators`) that
  speak the Ollama and OpenAI wire formats;
* Google Drive was replaced by an in-memory fake of the Drive v3 REST endpoints;
* S3 was replaced by `moto`;
* the container image is built only in CI;
* the Docker compute adapter ran against a real Docker Engine. The Kubernetes, Compute
  Engine and Azure adapters ran against in-memory fakes of their REST APIs, and EC2
  against moto. A local Kubernetes cluster (kind) could not start in the sandbox.

## Acceptance criteria

| # | Criterion | Evidence |
|---|---|---|
| 1 | Starts without a GPU | `test_application_starts_without_gpu_using_development_profile` (also asserts `torch` is not imported) |
| 2 | Mock inference end to end | `test_mock_inference_end_to_end`; e2e `same chat UI switches…` |
| 3 | Ollama without domain changes | Ollama added at runtime through the API and the UI (`test_same_request_shape_switches_mock_ollama_openai`, e2e); architecture boundary test |
| 4 | OpenAI-compatible endpoint | same tests; key resolved on the backend from `env:` reference |
| 5 | Provider switching without frontend changes | e2e test: same UI, providers listed from the API; `test_runtime_default_provider_switch_needs_no_client_change` |
| 6 | Meaningful errors for unavailable providers | `test_unavailable_provider_gives_meaningful_error_and_platform_keeps_working`, `test_upstream_http_errors_are_normalised`, e2e outage test |
| 7 | Streaming | SSE event order for all three adapters; SDK SSE parser tests |
| 8 | Cancellation | service-level tests; HTTP cancel endpoint **and** client disconnect, both verified to close the upstream stream; e2e *Stop* button |
| 9 | Capabilities control UI features | `Composer.test.tsx` (image upload only for `vision_input`, JSON mode only for `structured_output`); API returns 422 `capability_not_supported`; e2e checks the button appears only for a vision model |
| 10 | Storage contract tests | `tests/contract/test_storage_contract.py` — the same suite runs against local FS, Google Drive (fake) and S3 (moto), including resumable-upload recovery and corruption detection |
| 11 | Metadata survives restarts | `test_metadata_survives_restart` (models, runtime providers, overrides, conversations); migration upgrade test |
| 12 | Configuration changes validated | `tests/unit/test_config.py`, `test_runtime_configuration_changes_are_validated`, `test_settings_reject_unknown_keys_and_explicit_mode` |
| 13 | Credentials never in API responses | `test_credentials_never_appear_in_api_responses` (16 responses: all read endpoints, provider test, chat, error responses and OpenAPI); e2e checks the providers JSON |
| 14 | Benchmarks exportable | `test_benchmarks_run_and_export` (CSV, JSON, export to storage) |
| 15 | Colab notebook runs independently | `tests/integration/test_notebook.py`: static checks (no `omf` imports, no tunnels or keep-alive tricks, matches the platform's dataset and CSV columns) plus full execution in smoke mode, which needs torch; run by the `notebook` workflow |
| 16 | Platform works with Colab offline | `test_platform_functional_when_colab_offline`, `test_unauthorized_colab_endpoint_is_never_contacted` |

## Phase 1 — Architecture foundation

**Implemented.** Monorepo layout, domain model (`python/omf/core/domain`), ports
(`core/ports`), composition root with configuration profiles (`omf/bootstrap`), mock
adapter, FastAPI app with envelopes, error normalisation and request IDs, React app,
Dockerfile and Compose file, Makefile, CI. Architecture docs and ADRs 0001–0011.

**Decisions.** Modular monolith with hexagonal ports (ADR-0001). Pydantic in the domain
(ADR-0002). Explicit adapter catalog with no dynamic loading (ADR-0004).

## Phase 2 — Real model inference

**Implemented.** Ollama adapter (native API; capabilities from `/api/show`).
OpenAI-compatible adapter (covers vLLM, llama.cpp, TGI and others; ADR-0006). Provider
registry with hot reconfiguration and leases. Model discovery with a TTL cache. SSE
streaming with normalised events. Per-request provider selection. Capability evidence
(ADR-0003) and the `/deployments/verify` probe.

**Bugs found and fixed while testing.**
* A completed stream was recorded as "cancelled", because the consumer cancelled the
  producer during bookkeeping.
* The producer task was cancelled twice. The second native cancellation interrupted
  httpcore's shielded connection close, so generation kept running upstream.
* uvicorn and Starlette (ASGI 2.4) never noticed client disconnects. A disconnect
  watcher was added (ADR-0005).

## Phase 3 — Colab and storage

**Implemented.**
* Colab notebook with an offline smoke mode.
* Google Drive adapter: REST v3, resumable chunked uploads, `appProperties` checksums
  and versions, trash on delete, redirect host allowlist.
* Local filesystem adapter: hashed layout, atomic writes, version retention, corruption
  detection.
* S3-compatible adapter.
* Benchmark export to CSV and JSON, and to any storage provider.
* Colab compute adapter: manual, ephemeral, explicit offline/unknown states, and probes
  only endpoints the operator marked authorized (ADR-0007).
* Active and passive provider health monitoring.

**Limitations.**
* Drive is tested against a fake, not the real service.
* The notebook's Hub download and GPU paths have only been written, not executed here.

## Phase 4 — Platform features

**Implemented.**
* Benchmark Lab UI: cold and warm results, comparability notes (ADR-0011).
* Four routing policies. Fallback is explicit only, never after output has started,
  and never sends sensitive prompts to external providers.
* Conversation persistence, scoped per owner (migration 0002).
* Telemetry: structured logs plus optional OpenTelemetry, with OTel tested through
  in-memory exporters.
* API-key authentication with user and admin roles.
* Audit log.
* SSRF egress policy (ADR-0010).
* Request size limits and upload validation.

## Phase 5 — Open-source hardening

**Implemented.**
* Storage contract suite.
* Security review fixes found by reading the code:
  * opt-in prompt logging;
  * conversation ownership;
  * strict settings;
  * the correct error code for down providers;
  * the Drive redirect allowlist;
  * a JSON 404 instead of the SPA fallback for unknown API paths.
* CI with Python 3.11 and 3.13, PostgreSQL, web, contract drift, e2e and Docker build.
* Security workflow: pip-audit, pnpm audit, dependency review, and checks for committed
  weights and secrets.
* Release workflow: wheel, SDK, web bundle, GHCR image and GitHub release.
* Dependabot.
* README, CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, LICENSE (Apache-2.0) and NOTICE.
* Docs: deployment, troubleshooting, model compatibility and licensing.

**CI status.** All workflows were run on GitHub Actions and pass:
* CI: Python 3.11 and 3.13 with PostgreSQL 17, web, contract drift, e2e, and the Docker
  image build;
* Security;
* the notebook smoke run, which executes the notebook with CPU torch.

The first CI run found two real IPv6 problems that the IPv4-only sandbox had hidden:
* the egress policy rejected `::1` even when `allow_loopback` was set, because Python
  also classifies `::1` as "reserved";
* the e2e preview server bound to `localhost` (`::1`) while the test polled `127.0.0.1`.

Both are fixed. GitHub Actions are pinned to major-version tags, not commit SHAs;
Dependabot keeps them current, and you can pin SHAs for stricter supply-chain control.

## Phase 6 — Managed compute

**Implemented.**
* Adapters: `docker`, `kubernetes`, `aws_ec2`, `gcp_compute` and `azure_vm` (Azure:
  start/stop only).
* An admin-only, audited endpoint: `POST /api/v1/compute/{id}/{operation}`.
* Lifecycle buttons on the *Infrastructure* screen, shown only for operations the
  environment allows.
* The SDK method `computeOperation`.
* `configs/gpu-infrastructure.example.yaml` and ADR-0012.

**Safety model.**
* Lifecycle is opt-in (`allow_lifecycle`; otherwise 403 `lifecycle_disabled`).
* OMF labels or tags what it creates and refuses (409) to touch anything else.
* One operation at a time per environment.
* An environment whose secret is missing degrades to `unknown` instead of failing
  startup.

**Tests.**
* `tests/integration/test_compute_docker.py` runs against a real Docker Engine; it is
  skipped when there is no socket. It covers the full lifecycle with a reachable
  endpoint, the opt-in, refusal to touch a foreign container, and pull failure.
* `tests/integration/test_compute_cloud.py` runs against REST fakes and moto. It covers
  request shapes, GPU resources, the ownership refusals and state mapping.
* An API test covers 403, 502, 501, 404, 422, the degraded environment and audit
  entries.

**Found while building it.**
* The inline-secret scanner flagged `env_secret_refs: {HF_TOKEN: env:HF_TOKEN}` as a
  plain-text secret. Values in a `*_refs` map are now treated as references.
* Adapter config validation errors included the rejected input values (pydantic's
  default). A secret misplaced in the wrong field could therefore reach error details.
  The errors now omit input values.

## Phase 7 — Embeddings

**Implemented.**
* `POST /api/v1/embeddings` and the SDK method `embeddings`.
* `embed` as an optional port operation, with adapters for mock (`mock-embed`), Ollama
  (`/api/embed`) and OpenAI-compatible servers (`/embeddings`, which sorts by `index`).
* Engine output is validated: one finite vector per input, all the same size.
* Routing on the `embeddings` capability; the default chat model is never used.
* Inference records are tagged with `operation`. The metrics summary filters by
  operation, so embedding latencies do not distort chat percentiles.
* A verification probe for `embeddings`.

**Found while building it.**
* On OpenAI-compatible servers, an embedding model declared `[embeddings]` also kept the
  adapter's "listed model ⇒ chat" guess. It could therefore be routed chat traffic.
  Declarations now replace adapter guesses.
* The Docker compute adapter pulled the image on every provision, and Docker Hub rate
  limits made the integration test fail. It now pulls only when the image is missing
  (`pull_policy: if_not_present`, the default; `always` is available).

**Tests.** `tests/integration/test_embeddings.py` covers:
* each adapter, against the Ollama/OpenAI protocol emulators, which return pseudo-vectors
  in the real wire formats;
* vector validation;
* capability routing, the declaration override, the verify probe, and record/metrics
  tagging.

Both fixes were mutation-checked: reverting either one makes these tests fail. No real
embedding model was run.

## Phase 8 — Tool calling

**Implemented.**
* Requests accept `tools` and `tool_choice`. Responses return `tool_calls` with
  `finish_reason: tool_calls`. `tool` messages return results.
* Requests that use tools are routed on the `tool_calling` capability.
* In streams, tool calls are complete in `message.completed`.
* Tool calls and results are persisted in conversations.
* A verification probe for `tool_calling`.
* A Playground tool editor and tool-result form; the user runs the tools.
* Adapters: mock (deterministic calls from the schema), Ollama (generated ids, object
  arguments, `tool_name`, `done_reason` normalisation; `tool_choice` beyond
  `auto`/`none` returns 501) and OpenAI-compatible (fragment reassembly, with or without
  `index`).
* OMF never executes tools (ADR-0013).

**Wire formats.** The Ollama formats were checked against Ollama's current `docs/api.md`.
The OpenAI format follows the Chat Completions specification.

**Found while building it.**
* The mock's non-streaming path dropped tool calls; streaming worked.
* The OpenAI emulator crashed on `content: null` in assistant tool-call messages, which
  is valid OpenAI format.
* The `api.md` embeddings section had been inserted into the middle of the chat-request
  section; it has been moved.

**Tests.**
* `tests/integration/test_tools.py` (15 tests): validation, routing, the mock loop,
  persistence and the verify probe, and the Ollama and OpenAI wire formats against the
  emulators, including fragment reassembly and malformed calls.
* Web unit tests for the composer.
* A Playwright test of the full browser loop.

Two adapter behaviours were mutation-checked: OpenAI fragment reassembly and the Ollama
`tool_name` mapping. Breaking either one makes these tests fail. Real models were not run
here. The real-engine CI work runs separately.

## Real-engine tests

`tests/real_engine` runs the Ollama and OpenAI-compatible adapters, and the API, against
real engines on CPU. It covers:
* discovery (Ollama capabilities are `provider_reported`);
* chat, streaming with usage, and JSON mode;
* embeddings, including a loose check that paraphrases are closer than unrelated text;
* `verify`;
* cancellation through the cancel endpoint and through client disconnect.

The `real_engine` marker is deselected by default, so `pytest` and `make check` are
unchanged. `.github/workflows/real-engines.yml` runs the tests on demand, weekly, and
when they change:
* Ollama `0.40.0` with `qwen2.5:0.5b` and `all-minilm`;
* llama.cpp `server-b11515` with Qwen2.5-0.5B-Instruct Q4_K_M and nomic-embed-text-v1.5
  Q4_K_M from Hugging Face.

Models are cached between runs. The first run passed 7/7 on each engine. The engine
logs show `cancel task` for both cancellation paths, so cancelling stops generation in
the engine itself, not only in the API.

The tests found no difference between the emulators and the real engines. They check
behaviour, not byte-for-byte equality of payloads. These engines could not be run in the
development sandbox, because its proxy blocks Hugging Face and the Ollama registry.

## Phase 9 — SSO, key rotation, rate limits, shared cancellation

**Implemented** (ADR-0014):
* **OIDC bearer-token validation:**
  * JWKS discovery through the egress policy, with caching;
  * a refresh when a token names an unknown key id;
  * `iss`/`aud`/`exp` checks and an algorithm allowlist;
  * role mapping from a dotted claim path.
* **API keys:** `expires_at`, overlapping entries for rotation, and secrets re-read
  every `auth.token_reload_s`.
* **Rate limits:** per-principal token buckets for all calls and for inference, returning
  429 with `Retry-After`.
* **Shared cancellation** (`server.cancellation: shared`, migration 0003): a cancel sent
  to any replica stops the stream on the replica serving it.
* **Real-engine tool-calling tests:** a round trip and a streamed call against Ollama and
  llama.cpp, which runs with `--jinja`.

**Tests.**
* `tests/integration/test_auth.py` (10 tests), against a real loopback identity provider
  with generated RSA keys:
  * role mapping;
  * expired, wrong-audience, wrong-issuer and unknown-key tokens;
  * algorithm-confusion attacks (HS256 signed with the public key, and `alg: none`);
  * provider key rotation;
  * API-key rotation on disk and expiry;
  * rate limits per principal and per kind.
* `tests/integration/test_shared_cancellation.py`: two containers share one database.
  Cancelling through replica B stops replica A's stream, and another user cannot cancel
  it. Disabling the poller makes this test fail.
* A coordination contract test in the metadata suite, which runs on PostgreSQL in CI.
* Real-engine tool tests: when a tiny model answers in text instead of calling the tool,
  the test is reported as xfail, never as a silent pass.

## Storage backends: Azure Blob and MinIO

**Implemented.**
* An `azure_blob` adapter using REST over httpx, with no Azure SDK.
* Authentication: Shared Key, SAS token, or an Entra ID service principal.
* Block-blob uploads for large files.
* Metadata: checksums and versions are stored in blob metadata, and user metadata as
  base64 JSON.
* The egress policy is checked before the first request, and redirects are never
  followed.
* A storage provider whose secret is missing is registered as unavailable; startup
  still succeeds.
* Registered in the catalog. `STORAGE_CONFIG_MODELS` is shared with the profile tests.
* `configs/storage-backends.example.yaml` gives one profile with every storage adapter.

**Tested against real servers.**
* Azurite 3.37.0, locally and in the `Storage backends` workflow:
  * the full storage contract suite;
  * a real account SAS, plus a tampered SAS that is rejected;
  * a wrong account key, which is rejected; this proves Azurite verifies signatures;
  * upload, download, list and delete through the HTTP API.
* MinIO, in CI: the S3 adapter's contract suite passed 17/17. The image is
  `bitnamilegacy/minio:2025.7.23-debian-12-r5`, because `minio/minio` is no longer
  published on Docker Hub. It is a source build that reports
  `DEVELOPMENT.2025-07-23T15-54-02Z`.
* MinIO was not run locally, because the sandbox's anonymous Docker Hub pulls were
  rate-limited (HTTP 429).

**Shared Key format.** It follows azure-sdk-for-python's `SharedKeyCredentialPolicy`,
fetched from GitHub; learn.microsoft.com was blocked by the sandbox proxy. The header
collation tables were ported and compared value for value with the SDK source. A
hand-derived golden string-to-sign is in `tests/integration/test_azure_blob.py`.

**Not tested** against real Azure Storage accounts.

## Phase 10 — Audio and the embeddings UI

**Implemented.**
* `POST /api/v1/audio/transcriptions`, routed on `audio_input`. It checks the content
  type against an allowlist and the file's magic bytes, and enforces a 25 MB cap.
* `POST /api/v1/audio/speech`, routed on `audio_output`. It returns audio bytes.
* Optional port operations `transcribe` and `synthesize`.
* Adapters:
  * mock: a deterministic transcript and a WAV tone;
  * OpenAI-compatible: OpenAI audio wire format, with field names checked against the
    OpenAI OpenAPI specification;
  * Ollama: none, because Ollama has no audio API (checked against its `docs/api.md`).
* Records are tagged `transcription` or `speech`, and metrics filter by operation.
* SDK methods `transcribe` (FormData) and `speech` (Blob).
* New UI pages:
  * **Embeddings**: a cosine-similarity matrix.
  * **Audio**: upload or microphone recording for transcription, and text-to-speech
    with playback.

**Tests.**
* `tests/integration/test_audio.py`: the mock, upload validation, capability routing,
  magic-byte sniffing, the OpenAI wire format against the emulator (exact multipart
  and JSON fields), verbose transcription, and Ollama returning 501. Disabling the
  sniffing check or dropping the `language` field each make tests fail.
* Vitest tests for the matrix and deployment helpers.
* Playwright tests: the embeddings matrix, transcribing an uploaded WAV, and playing
  synthesized speech (the browser decodes it).

No real speech model was run. Only the mock and the OpenAI-compatible protocol
emulator were used. Microphone recording relies on the browser's MediaRecorder and
was not exercised in automated tests.

## Remaining work (not implemented)

* Server-side tool execution. This is deliberately out of scope; see ADR-0013.
* Compute adapters for ECS/Fargate, Cloud Run and Azure Container Apps. Azure VM
  provisioning (only start/stop exists). Runs of the Kubernetes and cloud adapters
  against real clusters and accounts.
* Multi-tenancy beyond per-principal conversation ownership: shared providers, settings
  and metrics.
* A login UI for OIDC; OMF validates tokens but runs no login flow (ADR-0014).
* Global (cross-replica) rate limits, and multi-replica deployments on SQLite.
* Runs of the storage adapters against real cloud accounts (Google Drive, AWS S3, Azure
  Storage). They are tested against fakes and emulators only.
