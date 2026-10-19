# ADR-0007: Colab is manually managed, ephemeral compute

**Status:** Accepted

## Context
Google Colab is an interactive notebook product. Its runtimes are ephemeral and its
resources are not guaranteed. Free runtimes restrict some uses as a remote web
backend. There is no supported API for unattended start, stop or keep-alive. OMF must
not build anything that works around those restrictions.

## Decision
- The `colab_notebook` compute adapter records operator-declared metadata: notebook URL,
  accelerator, GPU, RAM. Resources are marked `source: declared`. The environment is
  marked `ephemeral: true` and `managed: manual`.
- An inference endpoint may be registered by hand. OMF contacts it **only** when the
  operator sets `endpoint.authorized: true`, which attests that their Colab plan and the
  terms permit it. Unauthorized endpoints are recorded but never contacted (state
  `unknown`). The tests check that no request is made.
- States are explicit: no endpoint → `offline`; an authorized endpoint that is reachable
  → `online`, otherwise `offline`.
- `provision`, `start` and `stop` raise `OperationNotSupported` (HTTP 501). OMF has no
  reconnection, idle-timeout avoidance or resource-limit workarounds.
- Experiments run inside the notebook (`notebooks/colab_model_lab.ipynb`), which works
  on its own without the OMF API.

## Consequences
- The platform keeps working while Colab is offline. The tests check this.
- Colab cannot be a production inference backend in OMF. Use local or dedicated GPU
  infrastructure, registered as `openai_compatible` or `ollama`, for application
  traffic.

## Alternatives considered
- Tunnels with automatic reconnection: rejected (policy and reliability).
