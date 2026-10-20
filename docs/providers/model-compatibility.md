# Model compatibility

`configs/models.yaml` is the **model catalog**. It describes models independently of
where they run. Capabilities listed in the catalog are recorded as `declared`
(unverified). A deployment (the model on a specific provider) can gain stronger
evidence in two ways: the engine reports the capability (`provider_reported`), or OMF
probes it (`verified`). See [ADR-0003](../architecture/adr/0003-capability-evidence.md).

## Catalog matrix

| Catalog id | Params | Context | Input | Declared capabilities | Runtimes | Provider aliases |
|---|---|---|---|---|---|---|
| `qwen2.5-0.5b-instruct` | 0.5B | 32,768 | text | chat, text_generation, structured_output | transformers, ollama, vllm, llama.cpp | `qwen2.5:0.5b`, `qwen2.5:0.5b-instruct`, `Qwen/Qwen2.5-0.5B-Instruct` |
| `qwen2.5-1.5b-instruct` | 1.5B | 32,768 | text | chat, text_generation, structured_output | transformers, ollama, vllm, llama.cpp | `qwen2.5:1.5b`, `Qwen/Qwen2.5-1.5B-Instruct` |
| `qwen2.5-7b-instruct` | 7B | 131,072 | text | chat, text_generation, structured_output, tool_calling | transformers, ollama, vllm, llama.cpp | `qwen2.5:7b`, `Qwen/Qwen2.5-7B-Instruct` |
| `qwen2.5-vl-3b-instruct` | 3B | 32,768 | text, image | chat, text_generation, vision_input | transformers, vllm, ollama | `qwen2.5vl:3b`, `Qwen/Qwen2.5-VL-3B-Instruct` |
| `llama-3.2-1b-instruct` | 1B | 131,072 | text | chat, text_generation, tool_calling | transformers, ollama, vllm, llama.cpp | `llama3.2:1b`, `meta-llama/Llama-3.2-1B-Instruct` |
| `nomic-embed-text` | — | 8,192 | text → embedding | embeddings | ollama, sentence-transformers | `nomic-embed-text`, `nomic-embed-text:latest` |

The `revision` fields are left empty on purpose. Pin the Hugging Face commit you actually
tested, so benchmark runs record it.

Notes:

- `tool_calling` deployments accept `tools` in chat requests ([API](../architecture/api.md#tool-calling)).
  On OpenAI-compatible servers, declare it, and start the engine with tool parsing
  enabled (vLLM: `--enable-auto-tool-choice --tool-call-parser <parser>`; llama.cpp:
  `--jinja`). Ollama reports `tools` per model.
- `embeddings` deployments are served by `POST /api/v1/embeddings`. An embeddings-only
  deployment is rejected for chat (`capability_not_supported`) and is not offered in the
  Playground.
- The UI and the router only enable `vision_input` when the *deployment* has a claim for
  it. Ollama reports `vision` per model. On OpenAI-compatible servers it must be declared
  in the provider's `models:` list.

## How a deployment's capabilities are determined

| Source | Evidence | Example |
|---|---|---|
| Engine metadata | `provider_reported` | Ollama `/api/show` → `completion` ⇒ chat + text_generation; `vision` ⇒ vision_input; `tools` ⇒ tool_calling; `embedding` ⇒ embeddings. The list is treated as complete, so catalog declarations cannot add vision to a model Ollama says lacks it. |
| Adapter defaults | `declared` | OpenAI-compatible: a listed model ⇒ chat + text_generation. Ollama: completion models ⇒ structured_output (`format: json`). Streaming adapters ⇒ streaming for chat-capable deployments. Older Ollama without `capabilities` ⇒ chat + text_generation only. |
| Provider `models:` declarations | `declared` | `{name: Qwen/Qwen2.5-7B-Instruct, model_id: qwen2.5-7b-instruct, capabilities: [chat, structured_output]}`. When a model has only adapter defaults, an operator or catalog declaration *replaces* them. So `{name: BAAI/bge-small-en-v1.5, capabilities: [embeddings]}` on an OpenAI-compatible server is not also a chat model. |
| Catalog entry of the mapped model | `declared` | Matched by `model_id` or `provider_aliases` |
| Verification probe | `verified` | `POST /api/v1/deployments/verify` |

## Verifying a deployment

```bash
curl -s -X POST localhost:8080/api/v1/deployments/verify \
  -H 'content-type: application/json' \
  -d '{"provider_id": "ollama", "model": "qwen2.5:0.5b"}'
```

The probe sends a non-streaming request ("Reply with the single word: OK",
`max_tokens` 16). If streaming is claimed, it sends a streaming request. If
`structured_output` is claimed, it sends a JSON-mode request and parses the result.

| Probe result | Capabilities recorded as `verified` |
|---|---|
| Non-streaming request returns text | `chat`, `text_generation` |
| Streaming request returns deltas | `streaming` |
| JSON-mode response parses | `structured_output` |
| Two texts return two equal-length vectors | `embeddings` |
| A `get_weather` tool request returns a call with object arguments | `tool_calling` |

Embedding-only models get no chat probe. Small models often answer a tool request in
text; that is recorded as `failed`, not `verified`. Other claims (vision) are reported as
`not_probed` and keep their
current evidence. Results are persisted and survive restarts. A new probe replaces the
previous verified set for that deployment.

## Known-incompatible combinations

| Combination | Result |
|---|---|
| Images sent to a deployment without `vision_input` | 422 before any provider call |
| `response_format: json` on a deployment without `structured_output` | 422 |
| Prompt estimate + `max_tokens` larger than the deployment's context window | 422 `context_length_exceeded` |
