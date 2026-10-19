# syntax=docker/dockerfile:1.7
# Open Model Fabric: API + built web UI in one image (modular monolith).
# Build from the repository root:
#   docker build -f infrastructure/docker/Dockerfile -t open-model-fabric .

# ---------------------------------------------------------------- web build
FROM node:24.22.0-slim AS web
WORKDIR /src
RUN corepack enable && corepack prepare pnpm@10.18.0 --activate
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml .npmrc ./
COPY apps/web/package.json apps/web/
COPY packages/sdk-typescript/package.json packages/sdk-typescript/
COPY packages/contracts/package.json packages/contracts/
COPY examples/react-chat/package.json examples/react-chat/
RUN pnpm install --frozen-lockfile --filter @omf/web...
COPY packages/ packages/
COPY apps/web/ apps/web/
RUN pnpm --filter @omf/web exec vite build

# ---------------------------------------------------------------- python deps
FROM python:3.13.16-slim AS api-build
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --extra postgres --extra s3 --extra gdrive --extra otel
COPY python/ python/
COPY apps/api/ apps/api/
RUN uv sync --frozen --no-dev --no-editable --extra postgres --extra s3 --extra gdrive --extra otel

# ---------------------------------------------------------------- runtime
FROM python:3.13.16-slim AS runtime
LABEL org.opencontainers.image.title="open-model-fabric" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/findajay/Open-model-fabric"
RUN groupadd --system omf && useradd --system --gid omf --home /app --no-create-home omf \
    && mkdir -p /data && chown omf:omf /data
WORKDIR /app
COPY --from=api-build /app/.venv /app/.venv
COPY configs/ configs/
COPY --from=web /src/apps/web/dist /app/web
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    OMF_DATA_DIR=/data \
    OMF_CONFIG=/app/configs/docker.yaml
USER omf
EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=2).status == 200 else 1)"
CMD ["omf-api"]
