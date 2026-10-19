#!/usr/bin/env bash
# One-command local development: API (development profile, mock provider) + web UI.
# No GPU, no cloud account, no network access required.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/" >&2; exit 1; }
command -v pnpm >/dev/null || { echo "pnpm is required: corepack enable" >&2; exit 1; }

[ -d .venv ] || uv sync --all-extras
[ -d node_modules ] || pnpm install --frozen-lockfile

export OMF_PROFILE="${OMF_PROFILE:-development}"
.venv/bin/python -m omf_api &
API_PID=$!
trap 'kill $API_PID 2>/dev/null || true' EXIT INT TERM

for _ in $(seq 1 50); do
  curl -fs http://127.0.0.1:8080/health/live >/dev/null 2>&1 && break
  sleep 0.2
done
echo "API: http://127.0.0.1:8080/docs   Web UI: http://localhost:5173"
pnpm --filter @omf/web dev
