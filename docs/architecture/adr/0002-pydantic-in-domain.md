# ADR-0002: Pydantic models in the domain layer

**Status:** Accepted

## Context
Domain entities need validation (bounds, enums, discriminated content parts), JSON
serialisation (metadata store, API, SSE) and JSON Schema (adapter config schemas,
OpenAPI). Pure dataclasses would need a parallel set of DTOs and mappers for each of
those.

## Decision
Domain entities, value objects and stream events are pydantic v2 models. Pydantic counts
as a general-purpose library, not an infrastructure dependency. The architecture test
forbids web frameworks, HTTP clients, database drivers and provider SDKs in `omf.core`,
but allows pydantic.

## Consequences
- The same type validates config, travels across ports, is stored as a JSON document,
  and is returned by the API. The API layer adds only thin request and view DTOs
  (`omf_api/schemas.py`) where the public shape differs, for example `ProviderView`
  adding effective locality and secret-reference availability.
- The domain is coupled to the pydantic major version. Upgrades affect every layer.
- Some entities are mutable (`BenchmarkRun` is updated while a run progresses). Value
  objects that must not change (`CapabilityClaim`, `CapabilitySet`) are frozen.

## Alternatives considered
- Dataclasses + DTO mapping: purer, but a lot of duplicated code at this size.
- attrs + cattrs: similar trade-off, and less ecosystem fit with FastAPI.
