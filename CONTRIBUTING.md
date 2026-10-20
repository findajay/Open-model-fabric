# Contributing to Open Model Fabric

Thanks for helping! This project aims to stay a small, understandable modular monolith
with replaceable adapters. Please read [the architecture overview](docs/architecture/overview.md)
and the [ADRs](docs/architecture/adr/README.md) before larger changes.

## Development setup

```bash
make install   # Python deps with uv, Node deps with pnpm (from lockfiles)
make dev       # API + web UI, mock provider, no GPU needed
make help      # all tasks
```

## Before you open a pull request

```bash
make check     # ruff, ruff format, mypy --strict, eslint, prettier, tsc, all unit/integration tests,
               # OpenAPI/SDK drift check
make e2e       # browser tests (Playwright; starts the API, the web app and protocol emulators)
```

PostgreSQL tests run when `OMF_TEST_POSTGRES_URL` is set; CI always runs them.

Real-engine tests (`tests/real_engine`) are opt-in: start Ollama or llama.cpp, set the
`OMF_TEST_*` variables described in `tests/real_engine/conftest.py`, and run
`pytest -m real_engine`. The *Real engines* workflow runs them in CI on demand, weekly,
and whenever they change.

## Ground rules

* **Respect the boundaries.** `python/omf/core` must not import adapters, FastAPI,
  httpx, SQLAlchemy or any provider SDK (`tests/unit/test_architecture.py` enforces it).
* **New provider?** Implement the port, add a strict (`extra="forbid"`) config model,
  register a descriptor in `python/omf/bootstrap/catalog.py`, and add tests — against an
  emulator for network engines, and the shared contract suite for storage. See
  [developing adapters](docs/providers/developing-adapters.md). First check whether the
  `openai_compatible` adapter already covers the engine.
* **Never assume capabilities.** Only claim what the engine reports or what the operator
  declares; mark evidence correctly.
* **No secrets in configuration, code, tests or fixtures.** Use `*_ref` secret references.
* **No model weights in Git.** CI rejects weight file extensions.
* **No dynamic code loading** from configuration, and no plugin marketplace (ADR-0004).
* **API changes** must keep backward compatibility, or be versioned. Run `make sdk` to
  regenerate `packages/contracts/openapi.json` and the SDK types, and commit both.
* **Database changes** need an Alembic migration in
  `python/omf/adapters/metadata/migrations/versions` that works on SQLite and PostgreSQL.
* **Significant decisions** get an ADR (`docs/architecture/adr`).
* Keep pull requests focused, with tests, and describe what you verified.

## Commit and review

Use clear commit messages ("Add X", "Fix Y"). Every PR needs green CI and one maintainer
review. By contributing you agree that your contribution is licensed under the Apache
License 2.0.

## Code of Conduct

Participation is governed by our [Code of Conduct](CODE_OF_CONDUCT.md).
