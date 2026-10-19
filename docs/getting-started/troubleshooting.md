# Troubleshooting

Every API error has the shape
`{"error": {"code", "message", "request_id", "retryable", "details"}}`. The `code` is
stable; the `request_id` (also in the `X-Request-ID` response header) appears in the API
logs, so search the logs for it.

| Symptom / error code | Likely cause | Fix |
|---|---|---|
| `configuration_error: secrets must not be stored in configuration files` at startup | A key such as `api_key`, `token` or `password` has a literal value in YAML | Use `api_key_ref: env:NAME` (or `file:/run/secrets/name`) and set the variable on the API host |
| `configuration_error … auth.mode=none is only allowed when binding to localhost` | `server.host` is not loopback while auth is off | Use `127.0.0.1`, or configure `auth.mode: api_key` with a token |
| `configuration_error … environment variable X is not set and has no default` | `${X}` in a profile without `:-default` | Export `X` or add a default: `${X:-value}` |
| `egress_denied: loopback endpoints are disabled` | Provider URL points at `localhost` with `allow_loopback: false` | Development: set `security.egress.allow_loopback: true`. Production: use the service's network address |
| `egress_denied: cloud metadata endpoints are never allowed` | URL resolves to 169.254.169.254 & co. | Intentional SSRF protection; use the real endpoint |
| `provider_unavailable` (503) | Engine not running/reachable, DNS failure, or health check found it down | `curl` the base URL from the API host; *Providers → Health check*; check firewall/compose network |
| `provider_timeout` (504) | Model loading or generation slower than `timeouts.read_s`/`total_s` | Raise the provider's timeouts (first request to Ollama loads the model) |
| `provider_authentication_failed` (502) | Wrong/missing API key, or the secret reference cannot be resolved | Check `/api/v1/secrets` (admin) shows the reference as available; verify the key |
| `model_not_found` (404) | Model not served by that provider | *Model Registry → Rediscover models*; for Ollama `ollama pull <model>` |
| `capability_not_supported` (422) with `details.missing` | Request needs a capability the model does not declare (e.g. image → `vision_input`) | Pick a capable model, or declare it in the provider's `models:` list and run *Verify* |
| `no_route_available` (503) | No provider satisfies the policy, health and capabilities | `details.considered` lists every candidate and why it was skipped |
| `policy_violation` (403) | Request marked `sensitive` targeted an `external` provider | Intended. Use a `local` provider or set `allow_sensitive: true` deliberately |
| `context_length_exceeded` (422) | Estimated prompt + `max_tokens` exceed the model's context window | Shorten the conversation or lower `max_tokens` |
| `request_too_large` (413) | Body exceeds `server.max_request_bytes` | Raise the limit in the profile, or send fewer/smaller images |
| `authentication_required` (401) in the UI | API runs with `auth.mode: api_key` | *Settings → API access token* |
| Web UI shows "not connected" | API not running on :8080, or `OMF_API_URL` points elsewhere | `make dev-api`; Vite proxies `/api` to `OMF_API_URL` (default `http://127.0.0.1:8080`) |
| Streaming stops without `message.completed` | Reverse proxy buffering SSE | Disable buffering (`X-Accel-Buffering: no` is sent; for nginx set `proxy_buffering off`) |
| Benchmark marked *not comparable* | Targets differ in hardware, quantization or token-count source | Intended: compare like with like, or read the notes |
| Colab environment shows `offline` | No endpoint registered, or the notebook runtime stopped | Expected. The platform keeps working with other providers; the notebook runs standalone |
| Google Drive storage `down` | Missing `GDRIVE_SERVICE_ACCOUNT_JSON`, folder not shared with the service account | Share the folder with the service-account e-mail; check the secret reference |
| `database is locked` with SQLite | Several API processes share one SQLite file | Run one replica on SQLite, or use PostgreSQL (`metadata.url_ref`) |

## Logs

The API logs one line per request (method, path, status, duration, request id) and
structured JSON telemetry lines on the `omf.telemetry` logger. `OMF_LOG_LEVEL=DEBUG`
also logs every metric. Prompt text is never logged unless
`security.log_prompt_content: true`.

## Resetting local state

`make clean` removes `./var` (SQLite database and local artifacts) and build outputs.
