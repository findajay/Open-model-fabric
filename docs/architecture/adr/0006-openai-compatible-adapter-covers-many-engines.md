# ADR-0006: One OpenAI-compatible adapter for many engines

**Status:** Accepted

## Context
vLLM, Hugging Face Text Generation Inference (Messages API), llama.cpp `llama-server`,
LM Studio, LocalAI, SGLang and many hosted services implement
`/v1/chat/completions` and `/v1/models`. One adapter per engine would duplicate code
and multiply test effort.

## Decision
- One `openai_compatible` adapter. Engine differences are configuration:
  - `stream_usage`: send `stream_options.include_usage`;
  - `list_models`: whether `/models` exists (otherwise the declared `models` are used);
  - `extra_headers`: non-secret headers only; credentials must use `api_key_ref`;
  - `verify_tls`.
- `context_window` is read from `max_model_len`, `context_length` or `context_window`
  when the server reports one (vLLM reports `max_model_len`).
- Ollama keeps a **dedicated** adapter for its native API, because `/api/show` reports
  capabilities, context length and quantization that the OpenAI protocol cannot carry.
  Ollama's `/v1` endpoint can still be used through `openai_compatible`.
- The adapter defaults to `locality: external` (fail-safe for sensitive prompts).
  Operators mark self-hosted endpoints as `local`.

## Consequences
- New OpenAI-compatible engines work without code changes.
- Engines that need non-standard behaviour (multimodal quirks, unusual tool-call
  formats) may later need options or a new adapter. Tool calls use the standard
  `tools` / `tool_calls` shape, including streamed fragments ([ADR-0013](0013-tool-calling-passthrough.md)).
- Tests run against a protocol emulator (`tests/emulators/openai_emulator.py`), not a
  real vLLM server.

## Alternatives considered
- A dedicated adapter per engine: rejected (duplication).
- The `openai` Python SDK: an extra dependency with retries and behaviour we would need
  to override. httpx keeps error translation and egress checks under our control.
