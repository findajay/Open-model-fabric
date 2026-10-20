# Threat model and security controls

Scope: the OMF API process, its configuration, and the adapters it uses to reach
inference endpoints, storage and the metadata database. The React app is an untrusted
client of the API.

## Assets

- Provider credentials (API keys, service-account JSON, database URLs)
- Prompts and completions (may contain sensitive data)
- Platform configuration (providers, routing, settings) and the audit trail
- Stored artifacts (model files, experiment outputs, benchmark exports)
- Access to internal networks reachable from the API host

## Threats and implemented controls

| Threat | Control (where) |
|---|---|
| Credentials leaked via config files or Git | Secrets only as `*_ref` references (`env:` / `file:`). The profile loader rejects credential-looking keys with literal values (`bootstrap/config.py::find_inline_secrets`). Database URLs with embedded passwords are rejected (`metadata.url_ref` is used instead). `.gitignore` excludes `.env`, `var/` and model weight formats. |
| Credentials leaked via the API or logs | Secrets are resolved on the backend only, inside adapter factories. `SecretValue` redacts `repr`/`str`. API views return references and their availability (`ProviderView.secret_references`, `GET /api/v1/secrets`), never values. Headers that could carry credentials (`Authorization`, `Cookie`, `X-Api-Key`, …) are refused in `extra_headers`. `test_credentials_never_appear_in_api_responses` checks 16 responses, error responses included. |
| Arbitrary file read through `file:` secrets | `file:` references are confined to `security.secret_dirs` (default `/run/secrets`), and their size is capped. |
| Unauthenticated use of inference or management APIs | OIDC JWTs (`auth.oidc`; [ADR-0014](../architecture/adr/0014-sso-rotation-rate-limits.md)) checked as follows: the signature against the issuer's JWKS; `iss`, `aud` and `exp` with leeway; an algorithm allowlist with no `none` or HMAC, so key confusion is impossible (tested); and role mapping from a claim. API keys can have `expires_at` and are re-read for rotation. Bearer tokens with roles `user` and `admin`. Tokens are stored as SHA-256 digests and compared in constant time, and tokens shorter than 16 characters are ignored. `auth.mode: none` is rejected unless the server binds to localhost. Production profiles require `api_key` with at least one token. Health probes are the only unauthenticated routes. |
| Unauthorised configuration changes | Provider create/update/delete/test, settings changes, model registration and deletion, artifact upload and deletion, compute lifecycle, benchmark export-to-storage, audit log and secret listing all require `admin`. Security settings (auth, egress, CORS) cannot be changed at runtime. |
| SSRF through configurable endpoints | `EgressPolicy` (`core/security/egress.py`). Only http(s). No credentials in URLs. Cloud metadata hosts and IPs are always blocked, and so are link-local, multicast, reserved and unspecified addresses (IPv4-mapped IPv6 included). Loopback and private ranges need explicit opt-in. Optional host allowlist. It is checked when a provider is saved (including DNS) and again before connecting (cached 60 s). Redirects are not followed. Colab endpoints are contacted only when marked `authorized`. |
| Path traversal in artifact keys | `validate_key` (strict segment charset, no `.`, `..` or empty segments, no absolute paths). `local_fs` additionally maps keys to `sha256(key)` directories, so no key can address a path. |
| Oversized or abusive requests | Body size limit checked from `Content-Length` and enforced while streaming (`middleware.py`, 413). A separate, larger limit applies to artifact uploads. Pydantic bounds: at most 200 messages, at most 64 content parts, text up to 200k characters, images up to about 6 MB and of allowlisted types, `max_tokens` ≤ 131072. |
| Malicious or mislabeled audio uploads | `POST /api/v1/audio/transcriptions` accepts an allowlist of audio content types. It checks the file's magic bytes against the declared type and rejects mismatches before contacting any engine. Uploads are capped at 25 MB and by `max_request_bytes`. Speech responses from engines are capped at 50 MB, and non-audio content types are rejected. |
| Malicious uploads | Upload content-type allowlist (`security.allowed_upload_content_types`), key validation, size check. Content is checksummed and the SHA-256 is recorded. Downloads are re-verified. |
| Cross-origin abuse from browsers | CORS allowlist (`server.cors_allow_origins`). `*` is rejected in production. Credentials mode is off, and allowed methods and headers are restricted. |
| Information disclosure in errors | Normalised error shape. Unhandled exceptions return a generic `internal_error`. Validation errors do not echo input (tested). Upstream error messages are truncated to 300 characters. |
| Clickjacking / MIME sniffing / caching of API data | `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store` on every response. |
| Sensitive prompts sent to third parties | `sensitivity: sensitive` requests are never routed to providers whose effective `allow_sensitive` is false. `openai_compatible` defaults to `external`, so sensitive prompts are refused until an operator marks it local. There is no silent cross-provider fallback. |
| Prompt content in telemetry | `InferenceRecord`, metrics, spans and access logs carry ids, sizes and timings only. The access log omits query strings and bodies. |
| Repudiation of configuration changes | Audit log (`audit_log` table, `omf.audit` logger): actor, action, target, request id and changed field names. Values are not logged. |
| Model output triggering side effects (tool calls) | OMF never executes tools ([ADR-0013](../architecture/adr/0013-tool-calling-passthrough.md)). Tool calls are returned to the client as untrusted JSON text, and the client decides whether and how to run them. Tool definitions are bounded (64 per request, validated names, object schemas). |
| Abuse or runaway clients | Per-principal token buckets for all API calls and, separately, for inference (`security.rate_limits`). Exceeding a limit returns 429 `rate_limited` with `Retry-After`. Health probes are never limited. |
| Runaway cost from abandoned streams | Client disconnects cancel the upstream generation ([ADR-0005](../architecture/adr/0005-sse-streaming-and-cancellation.md)). Per-provider and global request timeouts apply. |

## Dependency scanning

Pinned Python dependencies (`pyproject.toml`, `uv.lock`) and npm dependencies (lockfile).
The CI workflow is meant to run `pip-audit` (in the dev dependency group) and
`npm audit`. Check `.github/workflows` for the current state.

## Known limitations

- **Fixed in this release:** the connect-time egress check cached its result with a
  `0.0` "never checked" sentinel compared against `time.monotonic()`. On a host that had
  been up for less than 60 seconds (e.g. a freshly started microVM), the first check per
  endpoint was skipped. Found by CI on a freshly booted runner; covered by a regression
  test.

- **DNS rebinding:** resolution and connection are separate steps. Mitigations: pin IP
  literals, put an egress proxy or Kubernetes `NetworkPolicy` in front of the API, keep
  `allowed_hosts` tight.
- The Google Drive `api_base` and S3 `endpoint_url` are checked syntactically against
  the egress policy, but not re-resolved at connect time. The Drive client follows
  redirects only to the API host, `*.googleapis.com` and `*.googleusercontent.com`.
- `security.log_prompt_content: true` emits an `omf.chat.prompt` telemetry event that
  contains the prompt text. It is off by default; leave it off outside debugging.
- Conversations are scoped to the principal that created them (admins can list all with
  `?all_users=true`). Requests for someone else's conversation get the same 404 as a
  missing id. Non-admins can only cancel their own in-flight requests. This is per-token
  isolation, not full multi-tenancy (shared providers, settings and metrics).
- Rate limits (`security.rate_limits`) are per API process. With N replicas the
  effective limit is up to N times the configured value. For hard global limits, add
  them at the gateway as well.
- API keys come from configuration. To rotate one without downtime:
  - add a second entry with the same name and a new secret;
  - deploy;
  - set `expires_at` on the old entry, or remove it.

  Secrets in files (e.g. Kubernetes Secrets) are re-read every `auth.token_reload_s`
  (default 60 s), so replacing the file rotates the key without a restart. There is no
  rotation API.
- OIDC: OMF validates bearer JWTs but does not run a login flow. The client (or a proxy
  such as oauth2-proxy) obtains the token. Revoked tokens stay valid until `exp`, because
  JWTs are not introspected. Keep provider access-token lifetimes short.
- Prompts and completions are stored in plain text in the metadata database when
  conversations are persisted. Use database-level encryption at rest.
- `auth.mode: none` trusts every local process on the host.
- In-flight cancellation is per process unless `server.cancellation: shared` is set. In
  shared mode a cancel reaches another replica within about 0.5 s, through the metadata
  database.

## Deployment hardening checklist

- Use `environment: production` with `auth.mode: api_key`. Provide tokens via
  `/run/secrets`.
- Terminate TLS in front of the API and log requests at the proxy. Set
  `security.rate_limits`, and add global limits at the gateway when you run several
  replicas.
- Prefer OIDC (`auth.oidc`) over long-lived API keys for people. Use API keys with
  `expires_at` for services.
- Keep `allow_loopback: false`. Enable `allow_private_networks` only with an
  `allowed_hosts` allowlist.
- Run as a non-root user with a read-only root filesystem. Mount only the data
  directories that need writing.
- Restrict outbound traffic with network policies to the inference endpoints, storage
  and database.
- Use PostgreSQL with TLS and encryption at rest. Back up the metadata store.
- Grant the Google Drive service account access to the single shared folder only
  (`drive.file` scope). Use IAM roles for S3 instead of static keys.
