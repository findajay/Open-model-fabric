# Architecture Decision Records

| # | Decision | Status |
|---|---|---|
| [0001](0001-modular-monolith-hexagonal.md) | Modular monolith with hexagonal architecture | Accepted |
| [0002](0002-pydantic-in-domain.md) | Pydantic models in the domain layer | Accepted |
| [0003](0003-capability-evidence.md) | Capability claims carry evidence; absent means unsupported | Accepted |
| [0004](0004-explicit-adapter-catalog-no-dynamic-loading.md) | Explicit adapter catalog, no dynamic loading | Accepted |
| [0005](0005-sse-streaming-and-cancellation.md) | SSE streaming with normalised events and task-based cancellation | Accepted |
| [0006](0006-openai-compatible-adapter-covers-many-engines.md) | One OpenAI-compatible adapter for many engines | Accepted |
| [0007](0007-colab-as-manually-managed-ephemeral-compute.md) | Colab is manually managed, ephemeral compute | Accepted |
| [0008](0008-google-drive-not-a-serving-store.md) | Google Drive stores artifacts, not a serving store | Accepted |
| [0009](0009-sqlalchemy-core-sqlite-postgres-alembic.md) | SQLAlchemy Core + Alembic for SQLite and PostgreSQL | Accepted |
| [0010](0010-secrets-by-reference-and-egress-policy.md) | Secrets by reference; egress (SSRF) policy | Accepted |
| [0011](0011-benchmark-comparability.md) | Benchmarks record environment and flag incomparable results | Accepted |

Format: Status · Context · Decision · Consequences · Alternatives considered.
New ADRs take the next number. Superseded ADRs stay in the index and are marked as such.
