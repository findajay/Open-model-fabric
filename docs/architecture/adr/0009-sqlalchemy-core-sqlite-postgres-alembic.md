# ADR-0009: SQLAlchemy Core + Alembic for SQLite and PostgreSQL

**Status:** Accepted

## Context
Metadata (providers, models, conversations, benchmarks, settings, audit, inference
records) must work with zero setup in development (SQLite) and scale in production
(PostgreSQL), with versioned schema migrations.

## Decision
- `SqlMetadataStore` uses synchronous SQLAlchemy Core in a worker thread
  (`asyncio.to_thread`). The workload is small, and one code path serves both databases.
- Portable schema: documents are stored as JSON text, and timestamps as ISO-8601 UTC
  strings, which sort correctly as text. Columns used for filtering or ordering are
  promoted out of the document.
- Alembic migrations (`adapters/metadata/migrations`) run on `initialize()` and are
  idempotent.
- SQLite runs with WAL mode, foreign keys on, a busy timeout, and an in-process write
  lock. PostgreSQL uses `postgresql+psycopg://`, with credentials from `metadata.url_ref`.

## Consequences
- The same tests (`tests/integration/test_metadata_store.py`) pass on SQLite and on
  PostgreSQL 16. The PostgreSQL run is gated by `OMF_TEST_POSTGRES_URL`.
- JSON documents cannot be queried efficiently by inner fields. Fields that need it get
  promoted columns in a new migration.
- A thread-pool hop per query. Acceptable at metadata volumes.

## Alternatives considered
- Async drivers (aiosqlite/asyncpg) with the SQLAlchemy async engine: two driver stacks
  for little gain.
- An ORM: unnecessary for document-style rows.
