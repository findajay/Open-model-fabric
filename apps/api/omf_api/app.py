"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from omf import __version__
from omf.bootstrap.config import AppConfig, load_config, resolve_config_path
from omf.bootstrap.container import Container, build_container
from omf_api.context import RequestIdLogFilter
from omf_api.errors import ErrorResponse, install_error_handlers
from omf_api.middleware import RequestContextMiddleware
from omf_api.routers import (
    benchmarks,
    chat,
    conversations,
    infrastructure,
    models,
    providers,
    system,
)
from omf_api.security import Authenticator

log = logging.getLogger(__name__)

DESCRIPTION = """
Provider-neutral AI application platform: one gateway in front of interchangeable
inference engines (mock, Ollama, any OpenAI-compatible server), with a model registry,
capability evidence, routing policies, streaming, benchmarks and pluggable storage.

All JSON responses use the envelope `{"data": ..., "meta": {"request_id": ...}}`; all
errors use `{"error": {"code", "message", "request_id", "retryable", "details"}}`.
"""


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if not any(isinstance(f, RequestIdLogFilter) for h in root.handlers for f in h.filters):
        handler = logging.StreamHandler()
        handler.addFilter(RequestIdLogFilter())
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s")
        )
        root.handlers = [handler]
    root.setLevel(level)


def create_app(
    config: AppConfig | None = None,
    *,
    container: Container | None = None,
    start_background: bool = True,
) -> FastAPI:
    cfg = config or (container.config if container else load_config(resolve_config_path()))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = container is None
        c = container or await build_container(cfg)
        app.state.container = c
        app.state.authenticator = Authenticator(cfg.auth, c.secrets)
        await c.start(background=start_background)
        log.info(
            "Open Model Fabric %s started (profile=%s, providers=%s)",
            __version__,
            cfg.profile,
            [p.id for p in c.providers.definitions()],
        )
        try:
            yield
        finally:
            if owned:
                await c.aclose()

    app = FastAPI(
        title="Open Model Fabric API",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        responses={"default": {"model": ErrorResponse}},
    )
    install_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.server.cors_allow_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID", "X-Content-SHA256"],
    )
    app.add_middleware(
        RequestContextMiddleware,
        max_request_bytes=cfg.server.max_request_bytes,
        max_upload_bytes=cfg.server.max_upload_bytes,
    )
    app.include_router(system.health_router)
    for module in (system, providers, models, chat, conversations, infrastructure, benchmarks):
        app.include_router(module.router)

    if cfg.server.serve_web_dir:
        _mount_web(app, Path(cfg.server.serve_web_dir))
    return app


def _mount_web(app: FastAPI, web_dir: Path) -> None:
    """Serve the built single-page app; unknown non-API paths fall back to index.html."""
    index = web_dir / "index.html"
    if not index.is_file():
        log.warning("serve_web_dir %s has no index.html; not serving the web app", web_dir)
        return
    app.mount("/assets", StaticFiles(directory=web_dir / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:  # sync: FastAPI runs it in a worker thread
        candidate = (web_dir / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(web_dir.resolve()):
            return FileResponse(candidate)
        return FileResponse(index)
