"""Tool calling: request validation, routing, mock adapter, and the Ollama and
OpenAI-compatible wire formats (against protocol emulators; no model runs).

OMF passes tools through: the model asks for a call, the *client* runs the tool and sends
the result back as a ``tool`` message. OMF never executes tools.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from omf.adapters.inference.openai_compatible import OpenAICompatibleConfig
from omf.adapters.inference.openai_compatible import create as create_openai
from omf.bootstrap.config import AppConfig
from omf.core.domain.errors import ProviderError
from omf.core.domain.models import (
    ChatMessage,
    InferenceRequest,
    ProviderDefinition,
    Role,
    ToolDefinition,
)
from tests.conftest import ServerThread, emulator_state, sse_events
from tests.integration.test_adapters import CTX

SECRET = "sk-test-0123456789abcdef-SECRET"
WEATHER = {
    "name": "get_weather",
    "description": "Current weather for a city",
    "parameters": {
        "type": "object",
        "properties": {"city": {"type": "string"}, "unit": {"enum": ["celsius", "fahrenheit"]}},
        "required": ["city", "unit"],
    },
}
CLOCK = {"name": "get_time", "parameters": {"type": "object", "properties": {}}}


def chat(client: TestClient, **body: Any) -> httpx.Response:
    body.setdefault("messages", [{"role": "user", "content": "Paris"}])
    return client.post("/api/v1/chat/completions", json=body)


def stream(client: TestClient, **body: Any) -> list[tuple[str, dict[str, Any]]]:
    body.setdefault("messages", [{"role": "user", "content": "Paris"}])
    with client.stream("POST", "/api/v1/chat/stream", json=body) as response:
        assert response.status_code == 200, response.read()
        return sse_events(response.iter_lines())


def follow_up(call: dict[str, Any], result: str) -> list[dict[str, Any]]:
    """The second turn of the loop: the client ran the tool and returns its result."""
    return [
        {"role": "user", "content": "Paris"},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": call["id"], "content": result},
    ]


# ---------------------------------------------------------------------- mock + API


def test_tool_call_round_trip_with_mock(
    make_config: Callable[..., AppConfig], make_client: Callable[..., TestClient]
) -> None:
    client = make_client(make_config())
    first = chat(client, tools=[WEATHER, CLOCK]).json()["data"]
    assert first["finish_reason"] == "tool_calls"
    [call] = first["message"]["tool_calls"]
    assert call["name"] == "get_weather"
    assert json.loads(call["arguments"]) == {"city": "Paris", "unit": "celsius"}

    forced = chat(client, tools=[WEATHER, CLOCK], tool_choice="get_time").json()["data"]
    assert forced["message"]["tool_calls"][0]["name"] == "get_time"
    plain = chat(client, tools=[WEATHER], tool_choice="none").json()["data"]
    assert plain["message"]["tool_calls"] == [] and plain["finish_reason"] == "stop"

    second = chat(client, tools=[WEATHER], messages=follow_up(call, "18 C, sunny")).json()["data"]
    assert second["finish_reason"] == "stop" and second["message"]["tool_calls"] == []
    assert "`get_weather` returned: 18 C, sunny" in second["message"]["content"]


def test_streamed_tool_calls_arrive_complete_in_message_completed(
    make_config: Callable[..., AppConfig], make_client: Callable[..., TestClient]
) -> None:
    client = make_client(make_config())
    events = stream(client, tools=[WEATHER])
    names = [name for name, _ in events]
    assert names[0] == "message.started" and names[-1] == "message.completed"
    assert "message.delta" not in names
    completed = events[-1][1]
    assert completed["finish_reason"] == "tool_calls"
    assert completed["tool_calls"][0]["name"] == "get_weather"
    # Plain streams still report an empty list.
    assert stream(client)[-1][1]["tool_calls"] == []


def test_tool_requests_route_only_to_tool_capable_models(
    make_config: Callable[..., AppConfig], make_client: Callable[..., TestClient]
) -> None:
    client = make_client(make_config())
    refused = chat(client, model="mock-vision", tools=[WEATHER])
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "capability_not_supported"
    assert refused.json()["error"]["details"]["missing"] == ["tool_calling"]
    # tool_choice "none" does not need the capability.
    assert chat(client, model="mock-vision", tools=[WEATHER], tool_choice="none").status_code == 200


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"tools": [WEATHER, WEATHER]}, "unique"),
        ({"tools": [WEATHER], "tool_choice": "get_time"}, "not one of the tools"),
        ({"tool_choice": "required"}, "needs tools"),
        ({"tools": [{**WEATHER, "name": "bad name!"}]}, "pattern"),
        ({"tools": [{**WEATHER, "parameters": {"type": "string"}}]}, "object"),
        (
            {"messages": [{"role": "tool", "tool_call_id": "call_x", "content": "r"}]},
            "unknown tool_call_id",
        ),
        ({"messages": [{"role": "tool", "content": "r"}]}, "need tool_call_id"),
        (
            {
                "messages": [
                    {"role": "user", "content": "x", "tool_calls": [{"id": "a", "name": "b"}]}
                ]
            },
            "only assistant",
        ),
    ],
)
def test_tool_request_validation(
    make_config: Callable[..., AppConfig],
    make_client: Callable[..., TestClient],
    body: dict[str, Any],
    fragment: str,
) -> None:
    response = chat(make_client(make_config()), **body)
    assert response.status_code == 422
    assert fragment in json.dumps(response.json())


def test_tool_calls_persist_in_conversations_and_verify_probe(
    make_config: Callable[..., AppConfig], make_client: Callable[..., TestClient]
) -> None:
    client = make_client(make_config())
    first = chat(client, tools=[WEATHER], persist=True).json()["data"]
    call = first["message"]["tool_calls"][0]
    # Continue the stored conversation: the call lives in history, not in this request.
    second = chat(
        client,
        tools=[WEATHER],
        conversation_id=first["conversation_id"],
        messages=[{"role": "tool", "tool_call_id": call["id"], "content": "rain"}],
    ).json()["data"]
    assert "`get_weather` returned: rain" in second["message"]["content"]
    stored = client.get(f"/api/v1/conversations/{first['conversation_id']}").json()["data"]
    roles = [m["role"] for m in stored["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert stored["messages"][1]["tool_calls"][0]["id"] == call["id"]
    assert stored["messages"][2]["tool_call_id"] == call["id"]

    verified = client.post(
        "/api/v1/deployments/verify", json={"provider_id": "mock", "model": "mock-chat"}
    ).json()["data"]
    checks = {c["capability"]: c for c in verified["checks"]}
    assert checks["tool_calling"]["result"] == "passed"


# ---------------------------------------------------------------------- Ollama


def test_ollama_tool_calls(
    make_config: Callable[..., AppConfig],
    make_client: Callable[..., TestClient],
    ollama_server: ServerThread,
) -> None:
    client = make_client(make_config())
    client.post(
        "/api/v1/providers",
        json={"id": "ollama", "adapter_type": "ollama", "config": {"base_url": ollama_server.url}},
    )
    target = {"provider_id": "ollama", "model": "qwen2.5:0.5b", "tools": [WEATHER]}
    data = chat(client, **target).json()["data"]
    # Ollama reports done_reason "stop" and no call ids; OMF normalises both.
    assert data["finish_reason"] == "tool_calls"
    call = data["message"]["tool_calls"][0]
    assert call["id"].startswith("call_") and call["name"] == "get_weather"
    assert json.loads(call["arguments"]) == {"city": "Paris", "unit": "celsius"}
    sent = emulator_state(ollama_server).requests[-1]
    assert sent["tools"][0] == {"type": "function", "function": WEATHER}

    completed = stream(client, **target)[-1][1]
    assert completed["finish_reason"] == "tool_calls"
    assert completed["tool_calls"][0]["name"] == "get_weather"

    answer = chat(client, **target, messages=follow_up(call, "18 C")).json()["data"]
    assert "get_weather=18 C" in answer["message"]["content"]
    sent = emulator_state(ollama_server).requests[-1]["messages"]
    # Arguments go back as an object; the result names the tool (Ollama has no ids).
    assert sent[1]["tool_calls"] == [
        {"function": {"name": "get_weather", "arguments": {"city": "Paris", "unit": "celsius"}}}
    ]
    assert sent[2] == {"role": "tool", "content": "18 C", "tool_name": "get_weather"}

    forced = chat(client, **target, tool_choice="get_weather")
    assert forced.status_code == 501
    assert forced.json()["error"]["code"] == "operation_not_supported"
    # /api/show says the vision model has no "tools": routing refuses before any call.
    vision = chat(client, provider_id="ollama", model="qwen2.5vl:3b", tools=[WEATHER])
    assert vision.json()["error"]["code"] == "capability_not_supported"


# ---------------------------------------------------------------------- OpenAI-compatible


def test_openai_compatible_tool_calls(
    make_config: Callable[..., AppConfig],
    make_client: Callable[..., TestClient],
    openai_server: ServerThread,
) -> None:
    client = make_client(make_config(), env={"OMF_TEST_REMOTE_KEY": SECRET})
    provider = {
        "id": "remote",
        "adapter_type": "openai_compatible",
        "config": {"base_url": f"{openai_server.url}/v1", "api_key_ref": "env:OMF_TEST_REMOTE_KEY"},
        "models": [
            {"name": "remote-small", "capabilities": ["chat", "text_generation", "tool_calling"]}
        ],
    }
    assert client.post("/api/v1/providers", json=provider).status_code == 201
    target = {"provider_id": "remote", "model": "remote-small", "tools": [WEATHER]}

    data = chat(client, **target).json()["data"]
    call = data["message"]["tool_calls"][0]
    assert data["finish_reason"] == "tool_calls"
    assert json.loads(call["arguments"]) == {"city": "Paris", "unit": "celsius"}

    # The emulator streams the arguments in fragments; OMF reassembles them exactly.
    completed = stream(client, **target)[-1][1]
    assert completed["tool_calls"][0]["arguments"] == call["arguments"]

    forced = chat(client, **target, tool_choice="get_weather").json()["data"]
    assert forced["message"]["tool_calls"][0]["name"] == "get_weather"
    sent = emulator_state(openai_server).requests[-1]
    assert sent["tool_choice"] == {"type": "function", "function": {"name": "get_weather"}}

    answer = chat(client, **target, messages=follow_up(call, "18 C")).json()["data"]
    assert "18 C" in answer["message"]["content"]
    sent = emulator_state(openai_server).requests[-1]["messages"]
    assert sent[1]["tool_calls"][0] == {
        "id": call["id"],
        "type": "function",
        "function": {"name": "get_weather", "arguments": call["arguments"]},
    }
    assert sent[1]["content"] is None
    assert sent[2] == {"role": "tool", "content": "18 C", "tool_call_id": call["id"]}


async def test_openai_stream_fragments_without_index_and_malformed_calls() -> None:
    def sse(*payloads: dict[str, Any]) -> bytes:
        return b"".join(f"data: {json.dumps(p)}\n\n".encode() for p in payloads) + (
            b"data: [DONE]\n\n"
        )

    def delta(calls: list[dict[str, Any]], finish: str | None = None) -> dict[str, Any]:
        return {"choices": [{"delta": {"tool_calls": calls}, "finish_reason": finish}]}

    bodies = {
        # No "index" (some servers omit it): fragments still assemble into one call.
        "noindex": sse(
            delta([{"id": "c1", "function": {"name": "get_", "arguments": '{"ci'}}]),
            delta([{"function": {"name": "weather", "arguments": 'ty": "X"}'}}], "tool_calls"),
        ),
        "noname": sse(delta([{"id": "c1", "function": {"arguments": "{}"}}], "tool_calls")),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=bodies[json.loads(request.content)["model"]])

    adapter = create_openai(
        ProviderDefinition(id="r", adapter_type="openai_compatible"),
        OpenAICompatibleConfig(base_url="http://127.0.0.1:9/v1"),
        CTX,
        transport=httpx.MockTransport(handler),
    )

    def request(model: str) -> InferenceRequest:
        return InferenceRequest(
            request_id="r1",
            model=model,
            messages=[ChatMessage(role=Role.USER, content="x")],
            tools=[ToolDefinition.model_validate(WEATHER)],
        )

    chunks = [c async for c in adapter.stream_generate(request("noindex"))]
    [call] = [c for chunk in chunks for c in chunk.tool_calls]
    assert (call.id, call.name, json.loads(call.arguments)) == ("c1", "get_weather", {"city": "X"})
    with pytest.raises(ProviderError, match="malformed tool call"):
        [c async for c in adapter.stream_generate(request("noname"))]
    await adapter.aclose()
