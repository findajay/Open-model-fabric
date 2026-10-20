# ADR-0003: Capability claims carry evidence; absent means unsupported

**Status:** Accepted

## Context
Models are not interchangeable: vision, tool calling, embeddings, JSON mode, context
limits and streaming all differ. Many engines report no capability metadata at all
(`/v1/models` in the OpenAI protocol). If OMF assumed capabilities it would send images
to text-only models, and the UI would show controls that fail.

## Decision
- A capability is supported **only if a claim exists**. Each claim records its `evidence`
  (`verified` > `provider_reported` > `declared`) and its `source`.
- Sources, merged by `ModelRegistryService` (strongest claim per capability wins):
  - adapter metadata, for example Ollama `/api/show` `capabilities` (provider_reported);
  - adapter defaults that follow from the protocol: a model listed by a chat-completions
    endpoint is `chat` *declared*, and streaming adapters add `streaming` *declared* for
    chat-capable deployments;
  - operator declarations in a provider's `models:` list, and catalog entries (declared);
  - probe results from `POST /api/v1/deployments/verify` (verified, persisted).
- When the engine reports an exhaustive list (`capabilities_complete`), declarations
  cannot add capabilities the engine leaves out.
- The router rejects or skips deployments missing a required capability. The UI adapts
  its controls to the claims.

## Consequences
- Discovered OpenAI-compatible models only support plain chat until the operator
  declares more or a probe verifies it. That is conservative by design.
- Verification probes chat, text_generation, streaming and, when claimed,
  structured_output, embeddings and tool_calling. Vision is not probed automatically and
  stays at its existing evidence level.
- An explicit declaration (provider `models:` or catalog) replaces an adapter's default
  guesses, but never claims an engine reported (amended with the embeddings endpoint).
- A new probe replaces all earlier `verified` claims for that deployment. A capability
  that fails re-verification falls back to its declared or reported evidence.

## Alternatives considered
- A static capability table per model family: goes stale and ignores runtime
  differences (e.g. llama.cpp without a vision projector).
- Assume everything and handle errors: fails at request time and leads to a poor UI.
