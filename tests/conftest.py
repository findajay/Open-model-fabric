"""Shared fixtures: real HTTP emulators on ephemeral ports, app factories, temp profiles."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient

from omf.bootstrap.config import AppConfig, parse_config
from omf_api.app import create_app
from tests.emulators import ollama_emulator, openai_emulator


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class ServerThread:
    """Runs an ASGI app with uvicorn in a background thread (real sockets)."""

    def __init__(self, app: Any) -> None:
        self.port = _free_port()
        self.app = app
        self.server = uvicorn.Server(
            uvicorn.Config(
                app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="on"
            )
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> ServerThread:
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *_: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def ollama_server() -> Iterator[ServerThread]:
    with ServerThread(ollama_emulator.create_app()) as server:
        yield server


@pytest.fixture
def openai_server() -> Iterator[ServerThread]:
    state = openai_emulator.EmulatorState(api_key="sk-test-0123456789abcdef-SECRET")
    with ServerThread(openai_emulator.create_app(state)) as server:
        yield server


def emulator_state(server: ServerThread) -> Any:
    return server.app.state.emulator


def base_config(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "profile": "test",
        "environment": "development",
        "auth": {"mode": "none"},
        "security": {"egress": {"allow_loopback": True}},
        "metadata": {"url": f"sqlite:///{tmp_path}/omf.db"},
        "compute": [{"id": "local", "adapter": "local", "config": {"detect_gpus": False}}],
        "storage": [
            {
                "id": "local",
                "adapter": "local_fs",
                "default": True,
                "config": {"root": str(tmp_path / "artifacts")},
            }
        ],
        "providers": [
            {
                "id": "mock",
                "adapter_type": "mock",
                "compute_environment_id": "local",
                "config": {"first_token_delay_ms": 1, "token_delay_ms": 1},
            }
        ],
        "inference": {"default_provider": "mock", "default_model": "mock-chat"},
        "health": {"interval_s": 0},
    }
    raw.update(overrides)
    return raw


@pytest.fixture
def make_config(tmp_path: Path) -> Callable[..., AppConfig]:
    def factory(environ: dict[str, str] | None = None, **overrides: Any) -> AppConfig:
        return parse_config(base_config(tmp_path, **overrides), environ or {})

    return factory


@pytest.fixture
def make_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def factory(config: AppConfig, env: dict[str, str] | None = None) -> TestClient:
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
        client = TestClient(create_app(config, start_background=False))
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for c in clients:
        c.__exit__(None, None, None)


def sse_events(lines: Iterator[str]) -> list[tuple[str, dict[str, Any]]]:
    import json

    events: list[tuple[str, dict[str, Any]]] = []
    name = None
    for line in lines:
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:") and name:
            events.append((name, json.loads(line[5:].strip())))
            name = None
    return events


def run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)
