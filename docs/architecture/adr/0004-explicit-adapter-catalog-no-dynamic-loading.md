# ADR-0004: Explicit adapter catalog, no dynamic loading

**Status:** Accepted

## Context
Configuration can come from YAML profiles and from admin API calls. If configuration
could name Python modules or classes, a configuration change would amount to code
execution. A plugin marketplace is out of scope.

## Decision
- Adapter types are listed explicitly in `python/omf/bootstrap/catalog.py`
  (`INFERENCE_ADAPTERS`, `COMPUTE_ADAPTERS`, `STORAGE_ADAPTERS`).
- Each inference adapter has an `InferenceAdapterDescriptor`: id, version, static
  capabilities, a pydantic config model (`extra="forbid"`), endpoint and secret fields,
  and a factory. Configuration selects an adapter by id and supplies schema-validated
  settings, nothing more.
- There are no entry points, no `importlib` from config, and no user code.

## Consequences
- A new adapter needs a code change and a release (one module plus one catalog entry).
  That is the intended trust boundary.
- `GET /api/v1/adapters` publishes every adapter's config JSON Schema, so the UI can
  render provider forms generically.
- Third-party adapters mean forking or adding a catalog entry. If this becomes a real
  need, a future ADR can add opt-in entry points restricted to installed packages.

## Alternatives considered
- Python entry points: safer than config imports, but adds discovery and versioning
  complexity before anyone needs it.
- Class paths in config: rejected (code execution through config).
