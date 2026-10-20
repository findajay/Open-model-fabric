# ADR-0013: Tool calling is passthrough; OMF never executes tools

**Status:** Accepted

## Context
Many models can ask to call functions ("tools"). A platform could either execute those
calls itself (an agent runtime) or pass them to the client. Executing tools inside a
gateway gives model output a path to side effects: network calls, file access, spending.
That path needs sandboxing, per-tool permissions and audit, which this project does not
provide. Engines also disagree on the wire format:
- OpenAI-compatible servers stream argument fragments keyed by `index`, with string
  arguments and call ids.
- Ollama sends complete calls with object arguments, no ids, no `tool_choice`, and
  `done_reason: stop`.

## Decision
- **Passthrough only.**
  - Requests carry `tools` (name, description, JSON Schema `parameters`) and
    `tool_choice`.
  - Responses carry `tool_calls` (`id`, `name`, `arguments` as the model's JSON text) with
    `finish_reason: tool_calls`.
  - The client executes the tool and sends `role: tool` messages with `tool_call_id`.
  - OMF never parses arguments to act on them.
- **One normalised shape.** Adapters convert engine formats:
  - generate ids when the engine has none;
  - convert object arguments to JSON text and back;
  - map `tool_call_id` to Ollama's `tool_name`;
  - reassemble streamed fragments.
- **Streaming.** Calls are delivered complete in `message.completed.tool_calls`, never
  as fragments. Text deltas stream as before. This is an additive change to the SSE
  contract.
- **Routing.** A request that offers tools (unless `tool_choice: none`) or contains tool
  messages requires the `tool_calling` capability, like any other capability
  (ADR-0003). Verification probes it with a `get_weather` tool.
- **Unsupported options fail loudly.** Ollama cannot force a tool, so `tool_choice:
  required` or a tool name returns 501 instead of being silently ignored.

## Consequences
- No new execution surface in the gateway: tool arguments are untrusted model output,
  and the client owns validation and execution.
- Clients write the tool loop. The Playground implements it for manual testing: the user
  types each result.
- Persisted conversations store tool calls and tool results, so a loop can continue via
  `conversation_id`.
- Small models often answer in text instead of calling the tool. The verification probe
  then records `failed`; it never upgrades the claim to `verified` without a real call.

## Alternatives considered
- **Server-side tool execution** (an agent runtime in OMF): rejected for now, because of
  the security surface. It could be a separate component that consumes this API.
- **Streaming argument fragments to clients:** this is lower latency for very long
  arguments, but clients would have to reassemble per engine. It was rejected for a
  single, complete shape.
