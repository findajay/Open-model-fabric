"""Adapters and API against real inference engines (Ollama, llama.cpp ``llama-server``).

Complements the emulator tests: the emulators pin the adapters to the documented wire
formats, these tests check that real engines actually behave that way.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from omf.adapters.inference.ollama import OllamaConfig
from omf.adapters.inference.ollama import create as create_ollama
from omf.adapters.inference.openai_compatible import OpenAICompatibleConfig
from omf.adapters.inference.openai_compatible import create as create_openai
from omf.bootstrap.config import AppConfig
from omf.core.domain.capabilities import Capability, Evidence
from omf.core.domain.models import (
    ChatMessage,
    EmbeddingRequest,
    GenerationParams,
    InferenceRequest,
    ProviderDefinition,
    ResponseFormat,
    Role,
    ToolCall,
    ToolDefinition,
)
from omf.core.ports.inference import InferenceProvider
from tests.conftest import ServerThread
from tests.integration.test_adapters import CTX
from tests.real_engine.conftest import Engine

pytestmark = pytest.mark.real_engine

SIMILAR = ("The cat sits on the mat.", "A cat is sitting on a mat.")
UNRELATED = "Quarterly revenue grew by eight percent in Europe."
LONG_PROMPT = "Count slowly from 1 to 300, one number per line, with no other text."


def adapter(provider: dict[str, Any]) -> InferenceProvider:
    definition = ProviderDefinition.model_validate(provider)
    if definition.adapter_type == "ollama":
        return create_ollama(definition, OllamaConfig.model_validate(provider["config"]), CTX)
    return create_openai(definition, OpenAICompatibleConfig.model_validate(provider["config"]), CTX)


def chat_request(model: str, text: str, **params: Any) -> InferenceRequest:
    return InferenceRequest(
        request_id=f"real-{time.monotonic_ns()}",
        model=model,
        messages=[ChatMessage(role=Role.USER, content=text)],
        params=GenerationParams(temperature=0, **params),
    )


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


# ---------------------------------------------------------------------- adapters


async def test_discovery(engine: Engine) -> None:
    chat = adapter(engine.chat_provider)
    found = {m.name: m for m in await chat.list_models()}
    assert engine.chat_model in found, sorted(found)
    claims = {c.capability: c for c in found[engine.chat_model].capabilities.claims}
    assert Capability.CHAT in claims
    if engine.reports_capabilities:
        # Ollama's /api/show capabilities, not an adapter guess.
        assert claims[Capability.CHAT].evidence == Evidence.PROVIDER_REPORTED
        embed = found[_ollama_name(engine.embed_model, found)].capabilities
        assert embed.supports(Capability.EMBEDDINGS) and not embed.supports(Capability.CHAT)
    else:
        assert claims[Capability.CHAT].evidence == Evidence.DECLARED
        embedder = adapter(engine.embed_provider)
        assert engine.embed_model in {m.name for m in await embedder.list_models()}
        await embedder.aclose()
    await chat.aclose()


def _ollama_name(model: str, found: dict[str, Any]) -> str:
    """Ollama lists ``all-minilm`` as ``all-minilm:latest``."""
    return model if model in found or ":" in model else f"{model}:latest"


async def test_non_streaming_chat(engine: Engine) -> None:
    chat = adapter(engine.chat_provider)
    result = await chat.generate(chat_request(engine.chat_model, "Say hello.", max_tokens=16))
    assert result.content.strip()
    assert result.finish_reason in ("stop", "length")
    assert result.usage is not None and result.usage.source == "provider"
    assert (result.usage.prompt_tokens or 0) > 0 and (result.usage.completion_tokens or 0) > 0
    assert result.usage.completion_tokens <= 16
    await chat.aclose()


async def test_streaming_deltas_and_final_usage(engine: Engine) -> None:
    chat = adapter(engine.chat_provider)
    deltas, finals = [], []
    async for chunk in chat.stream_generate(
        chat_request(engine.chat_model, "Write two short sentences about rivers.", max_tokens=48)
    ):
        if chunk.delta:
            deltas.append(chunk.delta)
        if chunk.finish_reason or chunk.usage:
            finals.append(chunk)
    assert len(deltas) >= 2  # really incremental, not one blob
    assert finals and finals[-1].finish_reason in ("stop", "length")
    usage = finals[-1].usage or next((f.usage for f in finals if f.usage), None)
    assert usage is not None, "engine sent no usage in the stream"
    assert (usage.completion_tokens or 0) > 0
    await chat.aclose()


async def test_json_mode_returns_parseable_json(engine: Engine) -> None:
    chat = adapter(engine.chat_provider)
    result = await chat.generate(
        chat_request(
            engine.chat_model,
            'Return a JSON object with a key "city" whose value is a city in France.',
            max_tokens=128,
            response_format=ResponseFormat.JSON,
        )
    )
    assert isinstance(json.loads(result.content), dict), result.content
    await chat.aclose()


async def test_embeddings(engine: Engine) -> None:
    embedder = adapter(engine.embed_provider)
    result = await embedder.embed(
        EmbeddingRequest(
            request_id="real-emb", model=engine.embed_model, inputs=[*SIMILAR, UNRELATED]
        )
    )
    a, b, c = result.vectors
    assert len(a) > 0 and len(a) == len(b) == len(c)
    # Loose semantic check: a real embedding model puts paraphrases closer together.
    assert cosine(a, b) > cosine(a, c), (cosine(a, b), cosine(a, c))
    await embedder.aclose()


# ---------------------------------------------------------------------- API


def _add_providers(client: TestClient, engine: Engine) -> None:
    for provider in engine.providers:
        response = client.post("/api/v1/providers", json=provider)
        assert response.status_code == 201, response.text


def test_full_api_path(
    engine: Engine,
    make_config: Callable[..., AppConfig],
    make_client: Callable[..., TestClient],
) -> None:
    client = make_client(make_config())
    _add_providers(client, engine)

    chat = client.post(
        "/api/v1/chat/completions",
        json={
            "provider_id": engine.chat_provider["id"],
            "model": engine.chat_model,
            "messages": [{"role": "user", "content": "Name one colour."}],
            "max_tokens": 16,
            "temperature": 0,
        },
    )
    assert chat.status_code == 200, chat.text
    data = chat.json()["data"]
    assert data["message"]["content"].strip() and data["usage"]["source"] == "provider"

    emb = client.post(
        "/api/v1/embeddings",
        json={"provider_id": engine.embed_provider["id"], "input": list(SIMILAR)},
    )
    assert emb.status_code == 200, emb.text
    assert len(emb.json()["data"]["data"]) == 2 and emb.json()["data"]["dimensions"] > 0

    verified = client.post(
        "/api/v1/deployments/verify",
        json={"provider_id": engine.chat_provider["id"], "model": engine.chat_model},
    )
    assert verified.status_code == 200, verified.text
    results = {c["capability"]: c["result"] for c in verified.json()["data"]["checks"]}
    assert results["chat"] == "passed" and results["streaming"] == "passed", results
    assert results.get("structured_output", "passed") == "passed", results

    embed_verified = client.post(
        "/api/v1/deployments/verify",
        json={
            "provider_id": engine.embed_provider["id"],
            "model": _api_embed_name(client, engine),
        },
    ).json()["data"]
    results = {c["capability"]: c["result"] for c in embed_verified["checks"]}
    assert results.get("embeddings") == "passed" and "chat" not in results, results


def _api_embed_name(client: TestClient, engine: Engine) -> str:
    deployments = client.get(
        f"/api/v1/deployments?provider_id={engine.embed_provider['id']}"
    ).json()["data"]
    names = [d["provider_model"] for d in deployments]
    for candidate in (engine.embed_model, f"{engine.embed_model}:latest"):
        if candidate in names:
            return candidate
    raise AssertionError(f"{engine.embed_model} not among {names}")


def test_cancellation_and_disconnect_end_the_stream(
    engine: Engine, make_config: Callable[..., AppConfig]
) -> None:
    from omf_api.app import create_app

    body = {
        "provider_id": engine.chat_provider["id"],
        "model": engine.chat_model,
        "messages": [{"role": "user", "content": LONG_PROMPT}],
        "max_tokens": 600,
        "temperature": 0,
    }
    with ServerThread(create_app(make_config(), start_background=False)) as api:
        for provider in engine.providers:
            httpx.post(f"{api.url}/api/v1/providers", json=provider, timeout=30).raise_for_status()

        # (a) explicit cancel endpoint: the stream ends with request.failed right away.
        names: list[str] = []
        failed: dict[str, Any] = {}
        cancelled_at = None
        with httpx.stream(
            "POST",
            f"{api.url}/api/v1/chat/stream",
            json=body,
            headers={"X-Request-ID": "real-cancel-1"},
            timeout=120,
        ) as r:
            event = None
            for line in r.iter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                    names.append(event)
                    if names.count("message.delta") == 3 and cancelled_at is None:
                        cancelled_at = time.monotonic()
                        res = httpx.post(f"{api.url}/api/v1/chat/requests/real-cancel-1/cancel")
                        assert res.json()["data"]["cancelled"] is True
                elif line.startswith("data:") and event == "request.failed":
                    failed = json.loads(line[5:])
        assert cancelled_at is not None, names
        assert names[-1] == "request.failed", names[-6:]
        assert failed["error"]["code"] == "request_cancelled"
        assert time.monotonic() - cancelled_at < 5

        # (b) client disconnect: the request is recorded as cancelled.
        with httpx.stream("POST", f"{api.url}/api/v1/chat/stream", json=body, timeout=120) as r:
            for line in r.iter_lines():
                if "message.delta" in line:
                    break
        deadline = time.monotonic() + 15
        statuses: list[str] = []
        while time.monotonic() < deadline:
            records = httpx.get(f"{api.url}/api/v1/requests/recent?limit=5").json()["data"]
            statuses = [rec["status"] for rec in records[:2]]
            if statuses == ["cancelled", "cancelled"]:
                break
            time.sleep(0.2)
        assert statuses == ["cancelled", "cancelled"], statuses

        # The engine is free again: had it kept generating 600 tokens for the abandoned
        # requests, this short request would queue behind them.
        started = time.monotonic()
        short = httpx.post(
            f"{api.url}/api/v1/chat/completions",
            json={**body, "messages": [{"role": "user", "content": "Say hi."}], "max_tokens": 8},
            timeout=120,
        )
        assert short.status_code == 200, short.text
        assert time.monotonic() - started < 20


# ---------------------------------------------------------------------- tool calling

WEATHER = ToolDefinition(
    name="get_weather",
    description="Get the current weather for a city.",
    parameters={
        "type": "object",
        "properties": {"city": {"type": "string", "description": "City name"}},
        "required": ["city"],
    },
)
TOOL_PROMPT = "What is the weather in Paris right now? Use the get_weather tool."


def tool_request(model: str, messages: list[ChatMessage]) -> InferenceRequest:
    return InferenceRequest(
        request_id=f"real-{time.monotonic_ns()}",
        model=model,
        messages=messages,
        params=GenerationParams(temperature=0, max_tokens=128),
        tools=[WEATHER],
    )


def _check_call(calls: list[ToolCall]) -> ToolCall:
    if not calls:
        # Tiny models sometimes answer in text. That is model behaviour, not a protocol
        # failure: report it visibly instead of passing silently.
        pytest.xfail("the model answered in text instead of calling the tool")
    call = calls[0]
    assert call.name == "get_weather"
    assert call.id  # generated by OMF for Ollama, by the server for OpenAI-compatible
    arguments = json.loads(call.arguments)
    assert isinstance(arguments, dict) and isinstance(arguments.get("city"), str)
    return call


async def test_tool_call_round_trip(engine: Engine) -> None:
    chat = adapter(engine.chat_provider)
    question = ChatMessage(role=Role.USER, content=TOOL_PROMPT)
    first = await chat.generate(tool_request(engine.chat_model, [question]))
    call = _check_call(first.tool_calls)
    assert first.finish_reason == "tool_calls"
    # The engine must accept our encoding of the assistant call and the tool result
    # (Ollama: object arguments + tool_name; OpenAI: tool_call_id, content null).
    answer = await chat.generate(
        tool_request(
            engine.chat_model,
            [
                question,
                ChatMessage(role=Role.ASSISTANT, content="", tool_calls=[call]),
                ChatMessage(
                    role=Role.TOOL,
                    tool_call_id=call.id,
                    content='{"temperature_c": 18, "conditions": "sunny"}',
                ),
            ],
        )
    )
    assert answer.content.strip() or answer.tool_calls
    await chat.aclose()


async def test_streamed_tool_call_is_reassembled(engine: Engine) -> None:
    chat = adapter(engine.chat_provider)
    calls: list[ToolCall] = []
    finish = None
    async for chunk in chat.stream_generate(
        tool_request(engine.chat_model, [ChatMessage(role=Role.USER, content=TOOL_PROMPT)])
    ):
        calls.extend(chunk.tool_calls)
        finish = chunk.finish_reason or finish
    _check_call(calls)  # arguments are complete, valid JSON after reassembly
    assert finish == "tool_calls"
    await chat.aclose()
