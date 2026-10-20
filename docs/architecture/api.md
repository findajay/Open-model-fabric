# API specification

The live specification is served at `/docs` (Swagger UI) and `/openapi.json`. A copy is
generated into `packages/contracts/openapi.json` with
`python -m omf_api.export_openapi` (or `make openapi`). The TypeScript SDK is built from
that file.

## Conventions

**Success envelope** (all JSON endpoints except health probes and exports):

```json
{"data": { ... }, "meta": {"request_id": "req_3bcc6a6e96364d9b9922"}}
```

**Error shape** (all errors):

```json
{"error": {"code": "provider_unavailable", "message": "provider 'ollama' is unreachable at ...",
           "request_id": "req_...", "retryable": true, "details": {"provider_id": "ollama"}}}
```

`code` is stable and part of the contract. [domain-model.md](domain-model.md#exceptions--http-status)
lists the status mapping. Validation errors list `loc`, `msg` and `type` and never echo
the submitted input.

**Request ids.** Send `X-Request-ID` (pattern `[A-Za-z0-9._:-]{1,100}`) to correlate a
request end to end; otherwise the server creates one. It is returned in the
`X-Request-ID` header, in `meta.request_id`, in error bodies, in log lines, and as the
inference `request_id`. For streams, it is the id you pass to the cancel endpoint.

**Authentication.** `Authorization: Bearer <token>`. Tokens come from
`auth.tokens[].token_ref`, and only their SHA-256 digests are kept in memory. Roles are
`user` and `admin`. With `auth.mode: none`, which only development profiles bound to
localhost allow, every caller is the admin `local-dev`. The `/health/*` probes are
unauthenticated.

**Limits.** The request body limit is `server.max_request_bytes` (default 10 MiB). Artifact
uploads use `server.max_upload_bytes`. Oversized bodies get 413 `request_too_large`.

## Endpoints

| Method & path | Role | Notes |
|---|---|---|
| `GET /health/live` | none | `{"status": "ok"}` |
| `GET /health/ready` | none | 200 if the metadata store is reachable and at least one provider is enabled, else 503. Provider outages do not fail readiness. |
| `GET /api/v1/system` | user | Version, profile, environment, auth mode, caller, adapter types |
| `GET /api/v1/adapters` | user | Adapter descriptors + config JSON Schemas (inference, compute, storage) |
| `GET /api/v1/providers` | user | `ProviderView[]`: definition, effective locality and `allow_sensitive`, secret *references* + availability, capabilities, health, `is_default` |
| `POST /api/v1/providers` | admin | Add a runtime provider (201). Validated, persisted, audited, health-checked. Returns warnings (e.g. unresolvable secret). |
| `POST /api/v1/providers/test` | admin | Body: `{provider_id}` **or** `{definition}`. Uses a throw-away adapter; nothing is persisted. |
| `GET /api/v1/providers/{id}` | user | |
| `PATCH /api/v1/providers/{id}` | admin | Config-origin providers accept only `enabled` |
| `DELETE /api/v1/providers/{id}` | admin | Runtime providers only |
| `POST /api/v1/providers/{id}/health` | user | Health check now |
| `GET /api/v1/providers/{id}/models?refresh=` | user | Deployments on this provider |
| `GET /api/v1/health/providers?refresh=` | user | Latest health of all providers |
| `GET /api/v1/models` | user | Catalog `ModelDefinition[]` |
| `POST /api/v1/models` | admin | Register a definition (capabilities recorded as declared) |
| `GET /api/v1/models/{model_id}` | user | Definition + its deployments (`model_id` may contain `/`) |
| `DELETE /api/v1/models/{model_id}` | admin | |
| `GET /api/v1/deployments?provider_id=&capability=&refresh=` | user | Deployments with capability claims and evidence |
| `POST /api/v1/deployments/verify` | user | `{provider_id, model}`: probes the deployment and records `verified` claims |
| `POST /api/v1/chat/completions` | user | `ChatRequest` → `InferenceResponse` |
| `POST /api/v1/chat/stream` | user | `ChatRequest` → `text/event-stream` |
| `POST /api/v1/chat/requests/{request_id}/cancel` | user | `{request_id, cancelled}` |
| `POST /api/v1/embeddings` | user | `EmbeddingsRequest` → `EmbeddingResponse` (see below) |
| `GET /api/v1/chat/requests` | user | Ids of in-flight requests |
| `GET/POST /api/v1/conversations` | user | List the caller's conversations (`limit` ≤ 200; admins: `all_users=true`) / create |
| `GET/PATCH/DELETE /api/v1/conversations/{id}` | user (owner) or admin | Get with messages / rename / delete; 404 for other users' conversations |
| `GET /api/v1/compute?refresh=` | user | Compute environments (cached until `refresh=true`) |
| `GET /api/v1/compute/{id}?refresh=` | user | Refreshes by default |
| `POST /api/v1/compute/{id}/{provision\|start\|stop\|deprovision}?wait=&timeout_s=` | admin | Audited. 403 `lifecycle_disabled` unless the environment sets `allow_lifecycle`; 501 if the adapter cannot (local, Colab); 409 for resources OMF does not own; 502 `compute_error` for backend failures |
| `GET /api/v1/storage` | user | `StorageStatus[]` |
| `GET /api/v1/storage/{sid}/artifacts?prefix=` | user | |
| `POST /api/v1/storage/{sid}/artifacts` | admin | Multipart `key` + `file`. Key, content type (allowlist) and size are validated. |
| `GET /api/v1/storage/{sid}/artifact?key=` | user | Metadata |
| `GET /api/v1/storage/{sid}/artifact/content?key=` | user | Checksum-verified download; `X-Content-SHA256` header |
| `POST /api/v1/storage/{sid}/artifact/verify?key=` | user | Re-hash |
| `DELETE /api/v1/storage/{sid}/artifact?key=` | admin | |
| `GET /api/v1/benchmarks/datasets` | user | Versioned prompt datasets (`smoke@1.0.0`, `short-generation@1.0.0`) |
| `POST /api/v1/benchmarks?wait=` | user | 202; runs in the background unless `wait=true` |
| `GET /api/v1/benchmarks`, `GET /api/v1/benchmarks/{id}` | user | |
| `POST /api/v1/benchmarks/{id}/cancel` | user | |
| `GET /api/v1/benchmarks/{id}/export?format=csv\|json` | user | CSV = one row per sample; JSON = `{"format": "omf.benchmark.v1", "run": ...}` |
| `POST /api/v1/benchmarks/{id}/export-to-storage?storage_id=&format=` | admin | Stores `benchmarks/{id}.{format}` (e.g. to Google Drive) |
| `GET /api/v1/settings` | user | `{runtime, read_only}` |
| `PATCH /api/v1/settings` | admin | Validated (unknown keys and `routing.mode: explicit` are rejected), persisted, audited |
| `GET /api/v1/requests/recent?limit=` | user | `InferenceRecord[]` (no prompt content) |
| `GET /api/v1/metrics/summary?window=&operation=chat\|embeddings` | user | Per-provider count, error rate, p50/p95 latency, mean TTFT, tokens/s, for one operation (default `chat`) |
| `GET /api/v1/metrics/telemetry` | user | In-process telemetry aggregates |
| `GET /api/v1/audit` | admin | |
| `GET /api/v1/secrets` | admin | Secret references from the profile and from all providers, with availability. Never values. |

### Chat request

```jsonc
{
  "messages": [{"role": "user", "content": "Hello"}],
  "provider_id": "ollama",            // optional; explicit routing, no fallback
  "model": "qwen2.5-0.5b-instruct",   // provider-native name or catalog id
  "conversation_id": null, "persist": false,
  "temperature": 0.2, "max_tokens": 256, "top_p": null, "stop": null, "seed": null,
  "response_format": "text",          // or "json" (requires structured_output)
  "sensitivity": "normal",            // "sensitive" never goes to external providers
  "required_capabilities": []
}
```

`content` can also be a list of parts:
`{"type": "text", "text": "..."}` and
`{"type": "image", "media_type": "image/png", "data_base64": "..."}`. Allowed image types
are png, jpeg, webp and gif, up to about 6 MB per image, with at most 64 parts per
message and 200 messages per request.

### Tool calling

OMF passes tools through to the model and returns the calls the model asks for. **OMF
never executes a tool.** The client runs it and sends the result back.

```jsonc
// 1. Offer tools. tool_choice: "auto" (default), "none", "required", or a tool's name.
{
  "messages": [{"role": "user", "content": "Weather in Paris?"}],
  "tools": [{"name": "get_weather", "description": "Current weather",
             "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                            "required": ["city"]}}]
}
// -> finish_reason "tool_calls";
//    message.tool_calls = [{"id": "call_1", "name": "get_weather", "arguments": "{\"city\": \"Paris\"}"}]

// 2. Return the result (repeat the assistant message, or continue via conversation_id).
{
  "messages": [
    {"role": "user", "content": "Weather in Paris?"},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "name": "get_weather", "arguments": "{\"city\": \"Paris\"}"}]},
    {"role": "tool", "tool_call_id": "call_1", "content": "18 C, sunny"}
  ],
  "tools": [ /* same tools */ ]
}
```

Rules:

- A request needs the `tool_calling` capability when it offers tools (unless
  `tool_choice` is `"none"`) or contains tool messages or tool calls.
- Tool names must be unique and match `^[a-zA-Z0-9_-]{1,64}$`. `parameters` must be a
  JSON Schema object.
- Without `conversation_id`, every `tool` message must answer a `tool_calls` id from an
  earlier assistant message in the same request.
- `arguments` is the JSON text the model produced. It is untrusted model output: validate
  it before running anything.
- When streaming, tool calls arrive complete in `message.completed.tool_calls`. Argument
  fragments are never streamed to clients.

Engine differences that OMF normalises:

- **Ollama** has no call ids, so OMF generates them. Ollama also takes arguments as an
  object, matches results by `tool_name`, and reports `done_reason: stop`; OMF reports
  `finish_reason: tool_calls`. Ollama has no `tool_choice`, so `required` or a named tool
  returns 501 `operation_not_supported`.
- **OpenAI-compatible** servers stream arguments as fragments keyed by `index`; OMF
  reassembles them. vLLM needs `--enable-auto-tool-choice` and a `--tool-call-parser`,
  and llama.cpp needs `--jinja`. Declare `tool_calling` for such models.

### Embeddings request

```jsonc
{
  "input": ["first text", "second text"],  // or a single string; 1-256 non-empty texts
  "provider_id": null,                     // optional; explicit routing, no fallback
  "model": null,                           // default: first model declaring `embeddings`
  "dimensions": null,                      // optional; only some models support it
  "sensitivity": "normal"
}
```

The response has `data: [{index, embedding}]` in input order, plus `dimensions`, `usage`,
`timings` and `route`. Routing requires the `embeddings` capability. The default *chat*
model is never used for embeddings. Adapters:

- `mock`: model `mock-embed`, 64-dimensional word-hashing vectors (no semantics);
- `ollama`: `POST /api/embed`;
- `openai_compatible`: `POST /embeddings`, for example vLLM with `--task embed`,
  llama.cpp with `--embeddings`, or TEI.

On OpenAI-compatible servers, declare the model as `capabilities: [embeddings]`, because
`/v1/models` cannot say which models embed.


## Server-Sent Events contract

Routing and validation errors are returned as ordinary HTTP errors *before* the stream
starts. After the stream starts, failures arrive as a terminal `request.failed` event.
Every stream ends with exactly one `message.completed` or `request.failed`.

```text
id: 0
event: message.started
data: {"request_id":"req_4540...","conversation_id":null,"provider_id":"mock","model":"mock-chat","model_id":null,"route":{"policy":"default","provider_id":"mock","model":"mock-chat","model_id":null,"attempts":[{"provider_id":"mock","model":"mock-chat","outcome":"selected","reason":null,"error_code":null},{"provider_id":"mock","model":"mock-chat","outcome":"succeeded","reason":null,"error_code":null}]}}

id: 1
event: message.delta
data: {"request_id":"req_4540...","index":0,"delta":"**Mock "}

id: 2
event: usage.reported
data: {"request_id":"req_4540...","usage":{"prompt_tokens":1,"completion_tokens":5,"total_tokens":6,"source":"provider"}}

id: 3
event: message.completed
data: {"request_id":"req_4540...","message_id":"msg_4e0a...","conversation_id":null,"finish_reason":"length","timings":{"total_ms":102.01,"ttft_ms":40.61,"tokens_per_second":81.43},"usage":{...},"tool_calls":[]}
```

A `: keep-alive` comment is sent after 15 s without events, for example while a model
loads. `usage.source` is `estimated` (about 4 characters per token) when the engine does
not report token counts.

## Cancellation

- **Explicit:** `POST /api/v1/chat/requests/{request_id}/cancel`. The service asks the
  provider for server-side cancellation where supported, then cancels the request task,
  which closes the upstream HTTP stream. The stream ends with
  `request.failed` / `request_cancelled`, and the record is stored as `cancelled`. If the
  request belonged to a conversation, the partial assistant text is stored with
  `metadata.status = "cancelled"`.
- **Client disconnect:** the server watches for `http.disconnect` and cancels the request
  the same way, so abandoned streams stop generating upstream
  ([ADR-0005](adr/0005-sse-streaming-and-cancellation.md)).
- A non-streaming request cancelled through the endpoint returns 409 `request_cancelled`.
