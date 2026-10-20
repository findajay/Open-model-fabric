"""Real inference engines (not emulators), selected by environment variables.

These tests are marked ``real_engine`` and deselected by default (see ``addopts`` in
pyproject.toml). Run them with ``pytest -m real_engine`` against running engines:

* ``OMF_TEST_OLLAMA_URL``: Ollama base URL, e.g. ``http://127.0.0.1:11434``.
  Models: ``OMF_TEST_OLLAMA_CHAT_MODEL`` (default ``qwen2.5:0.5b``) and
  ``OMF_TEST_OLLAMA_EMBED_MODEL`` (default ``all-minilm``), already pulled.
* ``OMF_TEST_LLAMACPP_URL`` and ``OMF_TEST_LLAMACPP_EMBED_URL``: llama.cpp
  ``llama-server`` instances (``/v1`` is appended), one for chat and one started with
  ``--embeddings``. Model names are the servers' ``--alias`` values:
  ``OMF_TEST_LLAMACPP_CHAT_MODEL`` (default ``qwen2.5-0.5b-instruct``) and
  ``OMF_TEST_LLAMACPP_EMBED_MODEL`` (default ``nomic-embed-text-v1.5``).

An engine whose variables are unset is skipped. Assertions check structure and
behaviour, never exact model output. ``.github/workflows/real-engines.yml`` runs them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import pytest

TIMEOUTS = {"connect_s": 10, "read_s": 300, "total_s": 600}


@dataclass
class Engine:
    name: str
    chat_provider: dict[str, Any]
    chat_model: str
    embed_provider: dict[str, Any]
    embed_model: str
    reports_capabilities: bool
    """Whether the engine itself reports model capabilities (``provider_reported``)."""
    providers: list[dict[str, Any]] = field(default_factory=list)


def _ollama() -> Engine | None:
    url = os.environ.get("OMF_TEST_OLLAMA_URL")
    if not url:
        return None
    provider = {
        "id": "ollama",
        "adapter_type": "ollama",
        "locality": "local",
        "timeouts": TIMEOUTS,
        "config": {"base_url": url},
    }
    return Engine(
        name="ollama",
        chat_provider=provider,
        chat_model=os.environ.get("OMF_TEST_OLLAMA_CHAT_MODEL", "qwen2.5:0.5b"),
        embed_provider=provider,
        embed_model=os.environ.get("OMF_TEST_OLLAMA_EMBED_MODEL", "all-minilm"),
        reports_capabilities=True,
        providers=[provider],
    )


def _llamacpp() -> Engine | None:
    chat_url = os.environ.get("OMF_TEST_LLAMACPP_URL")
    embed_url = os.environ.get("OMF_TEST_LLAMACPP_EMBED_URL")
    if not chat_url or not embed_url:
        return None
    chat_model = os.environ.get("OMF_TEST_LLAMACPP_CHAT_MODEL", "qwen2.5-0.5b-instruct")
    embed_model = os.environ.get("OMF_TEST_LLAMACPP_EMBED_MODEL", "nomic-embed-text-v1.5")
    chat = {
        "id": "llamacpp",
        "adapter_type": "openai_compatible",
        "locality": "local",
        "timeouts": TIMEOUTS,
        "config": {"base_url": f"{chat_url.rstrip('/')}/v1"},
        "models": [
            {
                "name": chat_model,
                # llama-server needs --jinja for tool calls (see the workflow).
                "capabilities": ["chat", "text_generation", "structured_output", "tool_calling"],
            }
        ],
    }
    embed = {
        "id": "llamacpp-embed",
        "adapter_type": "openai_compatible",
        "locality": "local",
        "timeouts": TIMEOUTS,
        "config": {"base_url": f"{embed_url.rstrip('/')}/v1"},
        "models": [{"name": embed_model, "capabilities": ["embeddings"]}],
    }
    return Engine(
        name="llamacpp",
        chat_provider=chat,
        chat_model=chat_model,
        embed_provider=embed,
        embed_model=embed_model,
        reports_capabilities=False,
        providers=[chat, embed],
    )


def _params() -> list[Any]:
    out = []
    for name, factory, hint in (
        ("ollama", _ollama, "OMF_TEST_OLLAMA_URL"),
        ("llamacpp", _llamacpp, "OMF_TEST_LLAMACPP_URL and OMF_TEST_LLAMACPP_EMBED_URL"),
    ):
        engine = factory()
        out.append(
            pytest.param(
                engine,
                id=name,
                marks=pytest.mark.skipif(engine is None, reason=f"{hint} not set"),
            )
        )
    return out


@pytest.fixture(params=_params())
def engine(request: pytest.FixtureRequest) -> Engine:
    value: Engine = request.param
    return value
