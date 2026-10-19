"""Embeddings: mock adapter, Ollama and OpenAI-compatible protocols (emulators), routing, API.

The emulators return deterministic pseudo-vectors in the real wire formats; they do not
run embedding models. Similarity is only meaningful for the mock adapter's word hashing.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from omf.adapters.inference._http import check_vectors
from omf.adapters.inference.mock import MockConfig, MockInferenceAdapter
from omf.adapters.inference.ollama import OllamaConfig
from omf.adapters.inference.ollama import create as create_ollama
from omf.adapters.inference.openai_compatible import OpenAICompatibleConfig
from omf.adapters.inference.openai_compatible import create as create_openai
from omf.bootstrap.config import AppConfig
from omf.core.domain.errors import CapabilityNotSupported, ProviderError
from omf.core.domain.models import EmbeddingRequest, ProviderDefinition
from tests.conftest import ServerThread, emulator_state
from tests.integration.test_adapters import CTX

SECRET = "sk-test-0123456789abcdef-SECRET"


def embed_request(model: str, *texts: str, dimensions: int | None = None) -> EmbeddingRequest:
    return EmbeddingRequest(request_id="e1", model=model, inputs=list(texts), dimensions=dimensions)


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


# ---------------------------------------------------------------------- adapters


async def test_mock_embeddings_are_deterministic_normalised_and_capability_checked() -> None:
    adapter = MockInferenceAdapter("mock", MockConfig(first_token_delay_ms=0))
    result = await adapter.embed(
        embed_request("mock-embed", "the cat sat", "a cat sat down", "quantum chromodynamics")
    )
    a, b, c = result.vectors
    assert len(a) == 64 and math.isclose(math.sqrt(cosine(a, a)), 1.0, abs_tol=1e-4)
    assert cosine(a, b) > cosine(a, c)  # shared words -> closer vectors
    again = await adapter.embed(embed_request("mock-embed", "the cat sat"))
    assert again.vectors[0] == a
    small = await adapter.embed(embed_request("mock-embed", "x", dimensions=8))
    assert len(small.vectors[0]) == 8
    with pytest.raises(CapabilityNotSupported):
        await adapter.embed(embed_request("mock-chat", "x"))
    models = {m.name: m for m in await adapter.list_models()}
    assert models["mock-embed"].capabilities.names() == ["embeddings"]  # no chat/streaming


async def test_ollama_embed(ollama_server: ServerThread) -> None:
    adapter = create_ollama(
        ProviderDefinition(id="ollama", adapter_type="ollama"),
        OllamaConfig(base_url=ollama_server.url),
        CTX,
    )
    result = await adapter.embed(
        embed_request("nomic-embed-text:latest", "first text", "second", dimensions=256)
    )
    assert [len(v) for v in result.vectors] == [256, 256]
    assert result.usage is not None and result.usage.prompt_tokens == 3
    sent = emulator_state(ollama_server).requests[-1]
    assert sent == {
        "model": "nomic-embed-text:latest",
        "input": ["first text", "second"],
        "dimensions": 256,
    }
    with pytest.raises(ProviderError, match="does not support embeddings"):
        await adapter.embed(embed_request("qwen2.5:0.5b", "x"))
    await adapter.aclose()


async def test_openai_compatible_embed_sorts_by_index(openai_server: ServerThread) -> None:
    adapter = create_openai(
        ProviderDefinition(id="remote", adapter_type="openai_compatible"),
        OpenAICompatibleConfig(base_url=f"{openai_server.url}/v1", api_key_ref="env:KEY"),
        CTX,
    )
    texts = ["alpha", "beta", "gamma"]
    result = await adapter.embed(embed_request("BAAI/bge-small-en-v1.5", *texts))
    from tests.emulators.ollama_emulator import fake_vector

    assert result.vectors == [fake_vector(t, 384) for t in texts]  # despite reversed order
    sent = emulator_state(openai_server).requests[-1]
    assert sent["encoding_format"] == "float" and "dimensions" not in sent
    assert emulator_state(openai_server).seen_auth_headers[-1] == f"Bearer {SECRET}"
    with pytest.raises(ProviderError):
        await adapter.embed(embed_request("remote-small", "x"))  # not an embedding model
    await adapter.aclose()


def test_vector_validation_rejects_malformed_engine_output() -> None:
    request = embed_request("m", "a", "b")
    for bad in (
        None,
        [[0.1]],
        [[0.1, 0.2], [0.3]],
        [["x"], ["y"]],
        [[], []],
        [[float("nan")], [1.0]],
    ):
        with pytest.raises(ProviderError):
            check_vectors("p", bad, request)
    assert check_vectors("p", [[1, 2], [3, 4]], request) == [[1.0, 2.0], [3.0, 4.0]]


# ---------------------------------------------------------------------- API


def embed(client: TestClient, **body: Any) -> Any:
    body.setdefault("input", ["hello world"])
    return client.post("/api/v1/embeddings", json=body)


def test_embeddings_api_routes_by_capability(
    make_config: Callable[..., AppConfig],
    make_client: Callable[..., TestClient],
    ollama_server: ServerThread,
) -> None:
    client = make_client(make_config())
    response = embed(client, input=["hello world", "hello there"])
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    # The default chat model (mock-chat) is not used: the first embeddings model is.
    assert data["model"] == "mock-embed" and data["dimensions"] == 64
    assert [d["index"] for d in data["data"]] == [0, 1]
    assert data["usage"]["prompt_tokens"] == 4

    single = embed(client, input="just one").json()["data"]
    assert len(single["data"]) == 1

    refused = embed(client, model="mock-chat")
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "capability_not_supported"
    for bad in ({"input": []}, {"input": ["  "]}, {"input": ["x"] * 257}, {"dimensions": 0}):
        assert embed(client, **bad).status_code == 422

    client.post(
        "/api/v1/providers",
        json={"id": "ollama", "adapter_type": "ollama", "config": {"base_url": ollama_server.url}},
    )
    via_ollama = embed(client, provider_id="ollama").json()["data"]
    assert via_ollama["model"] == "nomic-embed-text:latest" and via_ollama["dimensions"] == 768
    assert via_ollama["route"]["policy"] == "explicit"

    # Records are tagged, and chat statistics exclude embeddings.
    records = client.get("/api/v1/requests/recent").json()["data"]
    assert {r["operation"] for r in records} == {"embeddings"}
    chat_stats = client.get("/api/v1/metrics/summary").json()["data"]
    assert chat_stats["window_requests"] == 0
    emb_stats = client.get("/api/v1/metrics/summary?operation=embeddings").json()["data"]
    assert emb_stats["window_requests"] == len(records)


def test_declared_embedding_model_is_not_a_chat_model(
    make_config: Callable[..., AppConfig],
    make_client: Callable[..., TestClient],
    openai_server: ServerThread,
) -> None:
    client = make_client(make_config(), env={"OMF_TEST_REMOTE_KEY": SECRET})
    provider = {
        "id": "remote",
        "adapter_type": "openai_compatible",
        "config": {"base_url": f"{openai_server.url}/v1", "api_key_ref": "env:OMF_TEST_REMOTE_KEY"},
        "models": [{"name": "BAAI/bge-small-en-v1.5", "capabilities": ["embeddings"]}],
    }
    assert client.post("/api/v1/providers", json=provider).status_code == 201
    deployments = {
        d["provider_model"]: d
        for d in client.get("/api/v1/deployments?provider_id=remote").json()["data"]
    }
    embedder = deployments["BAAI/bge-small-en-v1.5"]
    assert [c["capability"] for c in embedder["capabilities"]["claims"]] == ["embeddings"]
    # Undeclared listed models keep the adapter's chat assumption.
    assert any(
        c["capability"] == "chat" for c in deployments["remote-small"]["capabilities"]["claims"]
    )

    chat = client.post(
        "/api/v1/chat/completions",
        json={
            "provider_id": "remote",
            "model": "BAAI/bge-small-en-v1.5",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert chat.status_code == 422 and chat.json()["error"]["code"] == "capability_not_supported"
    data = embed(client, provider_id="remote", dimensions=32).json()["data"]
    assert data["model"] == "BAAI/bge-small-en-v1.5" and data["dimensions"] == 32

    verified = client.post(
        "/api/v1/deployments/verify",
        json={"provider_id": "remote", "model": "BAAI/bge-small-en-v1.5"},
    ).json()["data"]
    results = {c["capability"]: c["result"] for c in verified["checks"]}
    assert results == {"embeddings": "passed"}  # no chat probe for an embedding-only model
    claims = verified["deployment"]["capabilities"]["claims"]
    assert claims[0]["evidence"] == "verified"
