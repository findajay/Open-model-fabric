# ADR-0005: SSE streaming with normalised events and task-based cancellation

**Status:** Accepted

## Context
Engines stream in different ways: OpenAI SSE `data:` chunks with `[DONE]`, Ollama
NDJSON, in-process generators. The browser needs one format. Users must be able to stop
generation, and a closed browser tab must not keep a GPU busy.

## Decision
- **Transport:** Server-Sent Events over a POST response. Browser clients read it with
  streaming `fetch`, because `EventSource` cannot POST. Adapters normalise their engine's format into
  `GenerationChunk` at the adapter boundary. `InferenceService` turns chunks into five
  events: `message.started`, `message.delta`, `usage.reported`, `message.completed`,
  `request.failed`. Exactly one terminal event is sent.
- **Errors before the stream** (routing, validation) are normal HTTP errors, thanks to
  a side-effect-free `preflight`. Errors after the stream starts become `request.failed`
  with `partial`.
- **Producer/consumer:** each request runs in its own asyncio task (the producer),
  registered under its request id. The task feeds a queue that the SSE response
  consumes. The adapter's generator is wrapped in `contextlib.aclosing`, so the upstream
  HTTP stream closes whenever iteration stops.
- **Cancellation:** `POST /chat/requests/{id}/cancel` calls the provider's optional
  `cancel()` and then cancels the producer task. Bookkeeping (the record, the partial
  message) is written before the terminal event is emitted.
- **Client disconnect watcher:** with ASGI spec 2.4 servers such as uvicorn, writes to a
  closed connection are dropped silently, and recent Starlette no longer listens for
  `http.disconnect` during `StreamingResponse`. Without a watcher, an abandoned stream
  would keep generating until it finished. `omf_api/sse.py` therefore runs a watcher on
  `request.receive()` that cancels the request on `http.disconnect`.
- **Cancel once:** the producer is never cancelled twice (`_cancel_once`, which checks
  `task.cancelling()`). Testing showed that a second native `task.cancel()` interrupts
  httpcore's connection close. That close runs inside an anyio shield, which does not
  block native asyncio cancellation. The upstream connection was left open and Ollama
  kept generating. The consumer also waits for the producer through `asyncio.shield`, so
  cancelling the HTTP response task cannot abort the producer's cleanup.
- **Keep-alive:** a `: keep-alive` comment is sent after 15 s of silence (model loading).

## Consequences
- Verified end to end over real sockets: both explicit cancel and client disconnect
  close the upstream stream. The protocol emulators count aborted streams
  (`test_cancellation_endpoint_and_disconnect_close_upstream_stream`).
- In-flight state is per process. In a multi-replica deployment, a cancel request must
  reach the replica that owns the stream (sticky sessions, or a shared signal later).
- SSE is one-way. WebSockets would only be needed for bidirectional features.

## Alternatives considered
- WebSockets: more complex through proxies, and not needed for one-way token streams.
- Passing provider-native streams through: would push format differences into every
  client.
