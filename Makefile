# Open Model Fabric — developer tasks. Run `make help`.
SHELL := /bin/bash
PY := .venv/bin/python
COMPOSE := docker compose -f infrastructure/compose/docker-compose.yml

.DEFAULT_GOAL := help
.PHONY: help install dev dev-api dev-web demo test test-py test-postgres test-web e2e notebook-test \
        lint format typecheck check openapi sdk contracts-check migrate audit build docker compose-up \
        compose-down clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install Python (uv) and Node (pnpm) dependencies from lockfiles
	uv sync --all-extras --frozen
	pnpm install --frozen-lockfile

dev: ## One command: API (mock provider, no GPU) + web UI at http://localhost:5173
	./scripts/dev.sh

dev-api: ## API only (OMF_PROFILE=development by default)
	OMF_PROFILE=$${OMF_PROFILE:-development} $(PY) -m omf_api

dev-web: ## Web UI only (expects the API on :8080)
	pnpm --filter @omf/web dev

demo: ## Provider-switch demo: mock -> Ollama -> OpenAI-compatible (emulators unless URLs given)
	$(PY) scripts/demo_provider_switch.py

test: test-py test-web ## All fast tests

test-py: ## Python unit, contract and integration tests
	$(PY) -m pytest

test-postgres: ## Metadata tests against PostgreSQL (needs OMF_TEST_POSTGRES_URL)
	$(PY) -m pytest -m postgres tests/integration/test_metadata_store.py

test-web: ## SDK and web unit tests
	pnpm -r test

e2e: ## Playwright end-to-end tests (starts API, web and emulators)
	pnpm --filter @omf/web e2e

notebook-test: ## Execute the Colab notebook in smoke mode (needs torch/transformers/nbclient)
	$(PY) -m pytest -m notebook tests/integration/test_notebook.py

lint: ## Ruff + ESLint + Prettier check
	.venv/bin/ruff check python apps/api tests scripts
	.venv/bin/ruff format --check python apps/api tests scripts
	pnpm -r lint
	pnpm format:check

format: ## Auto-format Python and TypeScript
	.venv/bin/ruff format python apps/api tests scripts
	.venv/bin/ruff check --fix python apps/api tests scripts
	pnpm format

typecheck: ## mypy (strict) + tsc
	.venv/bin/mypy
	pnpm -r typecheck

check: lint typecheck test contracts-check ## Everything CI runs except e2e

openapi: ## Regenerate packages/contracts/openapi.json from the FastAPI app
	$(PY) -m omf_api.export_openapi packages/contracts/openapi.json

sdk: openapi ## Regenerate the TypeScript SDK types from the OpenAPI contract
	pnpm --filter @omf/sdk generate

contracts-check: ## Fail if the OpenAPI contract or SDK types are stale
	$(PY) -m omf_api.export_openapi /tmp/omf-openapi-check.json
	diff -q packages/contracts/openapi.json /tmp/omf-openapi-check.json
	pnpm --filter @omf/sdk check-generated

migrate: ## Apply database migrations (OMF_DATABASE_URL or the active profile)
	$(PY) -m omf.bootstrap.migrate

audit: ## Dependency vulnerability scan (pip-audit + pnpm audit)
	.venv/bin/pip-audit --skip-editable --progress-spinner off
	pnpm audit --prod

build: ## Build the Python wheel and the web app
	uv build
	pnpm --filter @omf/web build

docker: ## Build the container image
	docker build -f infrastructure/docker/Dockerfile -t open-model-fabric:local .

compose-up: ## Start the compose stack (set OMF_ADMIN_TOKEN in .env); add PROFILE=ollama for Ollama
	$(COMPOSE) $(if $(PROFILE),--profile $(PROFILE)) up --build

compose-down: ## Stop the compose stack
	$(COMPOSE) down

clean: ## Remove local state and build outputs (never touches model weights elsewhere)
	rm -rf var apps/web/dist packages/sdk-typescript/dist apps/web/test-results apps/web/playwright-report \
	       .pytest_cache .mypy_cache .ruff_cache
