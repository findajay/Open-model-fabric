# ADR-0001: Modular monolith with hexagonal architecture

**Status:** Accepted

## Context
OMF must let users replace inference engines, compute, storage, metadata databases,
secrets and telemetry without touching business logic. It also has to run on a laptop
with no GPU and no cloud account. The team and the expected deployments are small.
Distributed services would add network failure modes, deployment work and operational
cost without solving any current problem.

## Decision
- Ship a single Python process: FastAPI gateway plus all application services.
- Use ports and adapters. `omf.core` (domain, ports, services) depends only on pydantic
  and the standard library. `omf.adapters` implements the ports. `omf.bootstrap` is the
  only composition root.
- Enforce the boundaries with a test (`tests/unit/test_architecture.py`) instead of
  relying on convention.
- Use dependency injection by construction: the `Container` builds services with their
  ports, and FastAPI handlers reach it through `request.app.state.container`.

## Consequences
- Adding a provider means adding an adapter and a catalog entry. Domain and services
  stay untouched (shown by the runtime-added Ollama and OpenAI-compatible providers in
  `tests/integration/test_api.py`).
- One deployable unit. Horizontal scaling is possible behind a load balancer when the
  metadata store is PostgreSQL. Two things are per-process: in-flight cancellation
  (cancelling needs to reach the process that owns the stream) and the health and
  discovery caches.
- Long-running benchmark tasks run inside the API process. A separate worker would
  become worth it if runs grow large.

## Alternatives considered
- **Microservices** (separate gateway, registry, benchmark service): rejected as
  premature.
- **Layered architecture without ports:** cheaper at first, but provider SDKs would leak
  into services.
