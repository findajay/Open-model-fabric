# Routing engine

Source: `python/omf/core/services/routing.py` (planning) and
`python/omf/core/services/inference.py` (execution and fallback).

## Policies

| Mode | Trigger | Candidates |
|---|---|---|
| `explicit` | Request sets `provider_id` | Exactly that provider. No fallback. Health is not consulted: the caller asked for it and gets a precise error. |
| `default` | `routing.mode: default` | `default_provider_id`, then `fallback_provider_ids` **only if** `allow_cross_provider_fallback: true` |
| `preferred_with_fallback` | `routing.mode` | First preferred provider. With `allow_cross_provider_fallback: true`, also the remaining preferred providers and then the fallback providers. |
| `capability_based` | `routing.mode` | All enabled providers that serve the model with the required capabilities, ranked healthy first, then `local` before `external`, then preferred order. Only the first is used unless cross-provider fallback is allowed. |

**Model resolution per candidate.** `model` may be a provider-native name or a catalog
id (catalog `provider_aliases` and declared `model_id` mappings link the two). If no model
is given, OMF uses `settings.default_model`. If that is not served by the candidate, it
uses the provider's first `available` deployment that has the required capabilities.
Cross-provider fallback only works for the **same** model. A provider-native name that
another provider does not serve is skipped.

**Required capabilities** are always `chat`, plus `streaming` (for streams),
`vision_input` (when the request has images), `structured_output` (for
`response_format: json`), plus any `required_capabilities` in the request.

## Safeguards

1. **Sensitive prompts.** A request with `sensitivity: sensitive` is never sent to a
   provider whose effective `allow_sensitive` is false. By default only `local`
   providers accept sensitive prompts, and `openai_compatible` defaults to `external`.
   Explicit selection of such a provider fails with 403 `policy_violation`.
2. **Fallback must be opted into.** Without `allow_cross_provider_fallback: true` there
   is one attempt. Config and settings validation reject `fallback_provider_ids` unless
   fallback is enabled.
3. **Retry eligibility.** The next candidate is tried only if the error is `retryable`
   (`provider_unavailable`, `provider_timeout`) **and** no output reached the caller. A
   stream never falls back after its first delta, so the client never sees two answers
   spliced together. `max_attempts` (default 2, max 5) caps the attempts. There is no
   same-provider retry.
4. **Health.** In non-explicit modes with `respect_health: true` (the default), providers
   currently `down` are skipped without being called (reported as `provider_unavailable`
   if no other candidate remains).
5. **Capabilities are authoritative.** A deployment without a claim for a required
   capability is skipped. In explicit mode this fails with 422 `capability_not_supported`
   and lists `missing` and `declared`.
6. **Context window.** If the deployment declares a context window, an estimate of
   prompt tokens (about 4 characters per token) plus `max_tokens` is checked first. If it
   is exceeded the request fails with 422 `context_length_exceeded`.
7. **Recorded decision.** Every response and `message.started` event includes a
   `route` with the policy and every attempt (`selected`, `skipped` with a reason,
   `failed` with an error code, `succeeded`). `InferenceRecord` stores the provider,
   model, policy and attempt count.

When nothing qualifies, the result is 503 `no_route_available` with the considered
candidates. When only one candidate was considered, its own precise error is returned
instead, such as `provider_unavailable` or `capability_not_supported`.

## Worked examples

**A. Default with no fallback (the shipped profiles).** The default provider `ollama` is
stopped. The request fails with 503 `provider_unavailable` (`retryable: true`). OMF does
*not* switch to the mock silently, because that would change answers without telling
anyone.

**B. Explicit fallback.**

```yaml
inference:
  default_provider: gpu-a
  routing:
    mode: default
    allow_cross_provider_fallback: true
    fallback_provider_ids: [gpu-b]
```

`gpu-a` times out → `gpu-b` serves the request. The `route.attempts` are
`selected(gpu-a)`, `selected(gpu-b)`, `failed(gpu-a, provider_timeout)` and
`succeeded(gpu-b)`. If `gpu-a` returns HTTP 400 (`provider_error`, not retryable), there
is no fallback.

**C. Mid-stream failure.** `gpu-a` drops the connection after three deltas. The stream
ends with `request.failed` and `partial: true`. `gpu-b` is not tried.

**D. Sensitive prompt with capability-based routing.** The providers are `cloud`
(external) and `onprem` (local). A request marked `sensitive` goes to `onprem`. If only
`cloud` serves the model, the result is `no_route_available`, and `cloud` shows in
`considered` with the `policy_violation` reason.

**E. Vision request to a text-only model.** The request sets
`provider_id: ollama`, `model: qwen2.5:0.5b` and includes an image. The result is 422
`capability_not_supported` with `missing: ["vision_input"]`. The UI hides image upload
for such models in the first place.

These behaviours are covered by `tests/unit/test_routing_and_inference.py` and
`tests/integration/test_api.py`.
