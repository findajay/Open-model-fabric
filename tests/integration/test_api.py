"""API-level acceptance tests against real-socket protocol emulators.

Covers acceptance criteria 1-9, 11, 13, 14 and 16. The Ollama/OpenAI emulators speak
the real wire protocols over HTTP; they do not run models (see tests/emulators).
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from omf.bootstrap.config import AppConfig, load_config
from tests.conftest import ServerThread, emulator_state, sse_events

ROOT = Path(__file__).resolve().parents[2]
SECRET = "sk-test-0123456789abcdef-SECRET"
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16).decode()


def chat(client: TestClient, **body: Any) -> httpx.Response:
    body.setdefault("messages", [{"role": "user", "content": "Hello there"}])
    return client.post("/api/v1/chat/completions", json=body)


def stream(client: TestClient, **body: Any) -> list[tuple[str, dict[str, Any]]]:
    body.setdefault("messages", [{"role": "user", "content": "Hello there"}])
    with client.stream("POST", "/api/v1/chat/stream", json=body) as response:
        assert response.status_code == 200, response.read()
        assert response.headers["content-type"].startswith("text/event-stream")
        return sse_events(response.iter_lines())


def add_provider(client: TestClient, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/api/v1/providers", json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]


# ---------------------------------------------------------------- 1 & 2: startup + mock


def test_application_starts_without_gpu_using_development_profile(
    tmp_path: Path, make_client: Callable[..., TestClient]
) -> None:
    config = load_config(ROOT / "configs" / "development.yaml", {"OMF_DATA_DIR": str(tmp_path)})
    client = make_client(config)
    assert client.get("/health/live").json() == {"status": "ok"}
    ready = client.get("/health/ready")
    assert ready.status_code == 200 and ready.json()["status"] == "ready"
    local = client.get("/api/v1/compute/local").json()["data"]
    assert local["state"] == "online"
    import sys

    assert "torch" not in sys.modules  # nothing pulled in a GPU stack


def test_mock_inference_end_to_end(
    make_config: Callable[..., AppConfig], make_client: Callable[..., TestClient]
) -> None:
    client = make_client(make_config())
    response = chat(client, persist=True)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["provider_id"] == "mock" and data["model"] == "mock-chat"
    assert "Hello there" in data["message"]["content"]
    assert data["usage"]["completion_tokens"] > 0 and data["timings"]["total_ms"] > 0
    assert (
        response.headers["x-request-id"]
        == data["request_id"]
        == response.json()["meta"]["request_id"]
    )
    conversation = client.get(f"/api/v1/conversations/{data['conversation_id']}").json()["data"]
    assert [m["role"] for m in conversation["messages"]] == ["user", "assistant"]
    # follow-up in the same conversation sends history to the provider
    follow = chat(
        client,
        conversation_id=data["conversation_id"],
        messages=[{"role": "user", "content": "and again"}],
    ).json()["data"]
    conversation = client.get(f"/api/v1/conversations/{follow['conversation_id']}").json()["data"]
    assert len(conversation["messages"]) == 4
    recent = client.get("/api/v1/requests/recent").json()["data"]
    assert {r["status"] for r in recent} == {"completed"}
    assert all("Hello" not in json.dumps(r) for r in recent)  # no prompt content in records


def test_client_supplied_request_id_is_propagated(
    make_config: Callable[..., AppConfig], make_client: Callable[..., TestClient]
) -> None:
    client = make_client(make_config())
    response = client.post(
        "/api/v1/chat/completions",
        headers={"X-Request-ID": "trace-abc-123"},
        json={"messages": [{"role": "user", "content": "x"}]},
    )
    assert response.headers["x-request-id"] == "trace-abc-123"
    assert response.json()["data"]["request_id"] == "trace-abc-123"


# ---------------------------------------------------------------- 3, 4, 5, 7: provider switching


def test_same_request_shape_switches_mock_ollama_openai(
    make_config: Callable[..., AppConfig],
    make_client: Callable[..., TestClient],
    ollama_server: ServerThread,
    openai_server: ServerThread,
) -> None:
    """The architecture acceptance test: one client payload, three engines, no code changes."""
    client = make_client(make_config(), env={"REMOTE_LLM_API_KEY": SECRET})
    add_provider(
        client,
        {"id": "ollama", "adapter_type": "ollama", "config": {"base_url": ollama_server.url}},
    )
    add_provider(
        client,
        {
            "id": "remote",
            "adapter_type": "openai_compatible",
            "locality": "external",
            "config": {
                "base_url": f"{openai_server.url}/v1",
                "api_key_ref": "env:REMOTE_LLM_API_KEY",
            },
        },
    )
    targets = [
        ("mock", "mock-chat", "Mock response"),
        ("ollama", "qwen2.5:0.5b", "Ollama emulator"),
        ("remote", "Qwen/Qwen2.5-0.5B-Instruct", "Remote OpenAI-compatible emulator"),
    ]
    for provider_id, model, marker in targets:
        payload = {
            "provider_id": provider_id,
            "model": model,
            "messages": [{"role": "user", "content": "What is 2+2?"}],
            "max_tokens": 64,
        }
        data = client.post("/api/v1/chat/completions", json=payload).json()["data"]
        assert data["provider_id"] == provider_id and marker in data["message"]["content"]
        events = stream(client, **payload)
        names = [e[0] for e in events]
        assert names[0] == "message.started" and names[-1] == "message.completed"
        assert "message.delta" in names and "usage.reported" in names
        text = "".join(e[1]["delta"] for e in events if e[0] == "message.delta")
        assert marker in text
        completed = events[-1][1]
        assert completed["usage"]["source"] == "provider"  # all three report real token counts
        assert completed["timings"]["ttft_ms"] is not None
    # the OpenAI-compatible provider received the key resolved on the backend
    assert f"Bearer {SECRET}" in emulator_state(openai_server).seen_auth_headers


def test_catalog_model_id_resolves_to_provider_native_names(
    make_config: Callable[..., AppConfig],
    make_client: Callable[..., TestClient],
    ollama_server: ServerThread,
) -> None:
    config = make_config(
        models=[
            {
                "id": "qwen2.5-0.5b-instruct",
                "display_name": "Qwen2.5 0.5B",
                "capabilities": ["chat", "text_generation"],
                "provider_aliases": ["qwen2.5:0.5b"],
            }
        ]
    )
    client = make_client(config)
    add_provider(
        client,
        {"id": "ollama", "adapter_type": "ollama", "config": {"base_url": ollama_server.url}},
    )
    data = chat(client, provider_id="ollama", model="qwen2.5-0.5b-instruct").json()["data"]
    assert data["model"] == "qwen2.5:0.5b" and data["model_id"] == "qwen2.5-0.5b-instruct"
    detail = client.get("/api/v1/models/qwen2.5-0.5b-instruct").json()["data"]
    assert [d["id"] for d in detail["deployments"]] == ["ollama/qwen2.5:0.5b"]


def test_runtime_default_provider_switch_needs_no_client_change(
    make_config: Callable[..., AppConfig],
    make_client: Callable[..., TestClient],
    ollama_server: ServerThread,
) -> None:
    client = make_client(make_config())
    add_provider(
        client,
        {"id": "ollama", "adapter_type": "ollama", "config": {"base_url": ollama_server.url}},
    )
    assert chat(client).json()["data"]["provider_id"] == "mock"
    response = client.patch(
        "/api/v1/settings", json={"default_provider_id": "ollama", "default_model": None}
    )
    assert response.status_code == 200, response.text
    data = chat(client).json()["data"]
    assert data["provider_id"] == "ollama" and data["model"] == "qwen2.5:0.5b"


# ---------------------------------------------------------------- 6 & 16: outages


def test_unavailable_provider_gives_meaningful_error_and_platform_keeps_working(
    make_config: Callable[..., AppConfig], make_client: Callable[..., TestClient]
) -> None:
    client = make_client(make_config())
    dead = "http://127.0.0.1:9"  # discard port: nothing listens
    add_provider(
        client,
        {
            "id": "ollama",
            "adapter_type": "ollama",
            "config": {"base_url": dead},
            "timeouts": {"connect_s": 1, "read_s": 2, "total_s": 5},
        },
    )
    view = client.get("/api/v1/providers/ollama").json()["data"]
    assert view["health"]["status"] == "down"
    assert view["health"]["error_code"] == "provider_unavailable"
    response = chat(client, provider_id="ollama", model="qwen2.5:0.5b")
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "provider_unavailable" and error["retryable"] is True
    assert "ollama" in error["message"] and error["request_id"]
    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={
            "provider_id": "ollama",
            "model": "qwen2.5:0.5b",
            "messages": [{"role": "user", "content": "x"}],
        },
    ) as r:
        assert r.status_code == 503
    # graceful degradation: mock still serves, readiness unaffected
    assert chat(client).status_code == 200
    assert client.get("/health/ready").status_code == 200


def test_upstream_http_errors_are_normalised(
    make_config: Callable[..., AppConfig],
    make_client: Callable[..., TestClient],
    openai_server: ServerThread,
) -> None:
    client = make_client(make_config(), env={"WRONG_KEY": "sk-wrong-key-000000000000"})
    add_provider(
        client,
        {
            "id": "remote",
            "adapter_type": "openai_compatible",
            "config": {"base_url": f"{openai_server.url}/v1", "api_key_ref": "env:WRONG_KEY"},
        },
    )
    response = chat(client, provider_id="remote", model="remote-small")
    # discovery is rejected with 401 -> the provider is reported unreachable/unauthorised
    assert response.status_code in (502, 503)
    assert response.json()["error"]["code"] in (
        "provider_authentication_failed",
        "provider_unavailable",
    )
    assert "sk-wrong-key" not in response.text


def test_platform_functional_when_colab_offline(
    make_config: Callable[..., AppConfig], make_client: Callable[..., TestClient]
) -> None:
    config = make_config(
        compute=[
            {"id": "local", "adapter": "local", "config": {"detect_gpus": False}},
            {
                "id": "colab",
                "adapter": "colab_notebook",
                "config": {
                    "declared_gpu": "T4",
                    "endpoint": {"url": "http://127.0.0.1:9", "authorized": True},
                },
            },
            {"id": "colab-unregistered", "adapter": "colab_notebook", "config": {}},
        ]
    )
    client = make_client(config)
    envs = {e["id"]: e for e in client.get("/api/v1/compute?refresh=true").json()["data"]}
    assert envs["colab"]["state"] == "offline" and envs["colab"]["ephemeral"] is True
    assert envs["colab"]["managed"] == "manual"
    assert envs["colab-unregistered"]["state"] == "offline"
    assert envs["colab"]["endpoints"][0]["reachable"] is False
    response = client.post("/api/v1/compute/colab/start")
    assert (
        response.status_code == 501
        and response.json()["error"]["code"] == "operation_not_supported"
    )
    assert chat(client).status_code == 200
    assert client.get("/health/ready").status_code == 200


def test_unauthorized_colab_endpoint_is_never_contacted(
    make_config: Callable[..., AppConfig],
    make_client: Callable[..., TestClient],
    openai_server: ServerThread,
) -> None:
    config = make_config(
        compute=[
            {"id": "local", "adapter": "local"},
            {
                "id": "colab",
                "adapter": "colab_notebook",
                "config": {"endpoint": {"url": openai_server.url, "authorized": False}},
            },
        ]
    )
    client = make_client(config)
    env = client.get("/api/v1/compute/colab").json()["data"]
    assert env["state"] == "unknown" and env["endpoints"][0]["reachable"] is None
    assert emulator_state(openai_server).seen_auth_headers == []  # no request was made


# ---------------------------------------------------------------- 8: cancellation over HTTP


def test_cancellation_endpoint_and_disconnect_close_upstream_stream(
    make_config: Callable[..., AppConfig], ollama_server: ServerThread
) -> None:
    from omf_api.app import create_app

    emulator_state(ollama_server).token_delay_s = 0.05
    config = make_config()
    with ServerThread(create_app(config, start_background=False)) as api:
        httpx.post(
            f"{api.url}/api/v1/providers",
            json={
                "id": "ollama",
                "adapter_type": "ollama",
                "config": {"base_url": ollama_server.url},
            },
        ).raise_for_status()
        body = {
            "provider_id": "ollama",
            "model": "qwen2.5:0.5b",
            "messages": [{"role": "user", "content": "tell me a long story " * 5}],
        }
        # (a) explicit cancel endpoint
        with httpx.stream(
            "POST",
            f"{api.url}/api/v1/chat/stream",
            json=body,
            headers={"X-Request-ID": "cancel-me-1"},
            timeout=10,
        ) as r:
            names = []
            cancelled = False
            for line in r.iter_lines():
                if line.startswith("event:"):
                    names.append(line[6:].strip())
                    if names.count("message.delta") == 2 and not cancelled:
                        cancel = httpx.post(f"{api.url}/api/v1/chat/requests/cancel-me-1/cancel")
                        assert cancel.json()["data"]["cancelled"] is True
                        cancelled = True
            assert names[-1] == "request.failed"
        # (b) client disconnect
        with httpx.stream("POST", f"{api.url}/api/v1/chat/stream", json=body, timeout=10) as r:
            for line in r.iter_lines():
                if "message.delta" in line:
                    break
        deadline = time.time() + 5
        while emulator_state(ollama_server).aborted_streams < 2 and time.time() < deadline:
            time.sleep(0.05)
        assert (
            emulator_state(ollama_server).aborted_streams == 2
        )  # both upstream streams were closed
        records = httpx.get(f"{api.url}/api/v1/requests/recent").json()["data"]
        assert [r["status"] for r in records[:2]] == ["cancelled", "cancelled"]


# ---------------------------------------------------------------- 9: capabilities


def test_capabilities_are_authoritative(
    make_config: Callable[..., AppConfig],
    make_client: Callable[..., TestClient],
    ollama_server: ServerThread,
    openai_server: ServerThread,
) -> None:
    client = make_client(make_config(), env={"K": SECRET})
    add_provider(
        client,
        {"id": "ollama", "adapter_type": "ollama", "config": {"base_url": ollama_server.url}},
    )
    add_provider(
        client,
        {
            "id": "remote",
            "adapter_type": "openai_compatible",
            "config": {"base_url": f"{openai_server.url}/v1", "api_key_ref": "env:K"},
        },
    )
    deployments = {d["id"]: d for d in client.get("/api/v1/deployments").json()["data"]}

    def caps(dep_id: str) -> dict[str, str]:
        return {
            c["capability"]: c["evidence"] for c in deployments[dep_id]["capabilities"]["claims"]
        }

    assert caps("ollama/qwen2.5vl:3b")["vision_input"] == "provider_reported"
    assert "vision_input" not in caps("ollama/qwen2.5:0.5b")
    assert "chat" not in caps("ollama/nomic-embed-text:latest")  # embedding-only model
    assert caps("ollama/nomic-embed-text:latest")["embeddings"] == "provider_reported"
    assert caps("remote/remote-small") == {
        "chat": "declared",
        "text_generation": "declared",
        "streaming": "declared",
    }  # nothing assumed beyond chat
    image_msg = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this?"},
                {"type": "image", "media_type": "image/png", "data_base64": PNG},
            ],
        }
    ]
    rejected = chat(client, provider_id="ollama", model="qwen2.5:0.5b", messages=image_msg)
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "capability_not_supported"
    assert rejected.json()["error"]["details"]["missing"] == ["vision_input"]
    accepted = chat(client, provider_id="ollama", model="qwen2.5vl:3b", messages=image_msg)
    assert (
        accepted.status_code == 200 and "1 image" in accepted.json()["data"]["message"]["content"]
    )
    assert chat(client, provider_id="ollama", model="nomic-embed-text:latest").status_code == 422


def test_capability_verification_is_recorded(
    make_config: Callable[..., AppConfig],
    make_client: Callable[..., TestClient],
    openai_server: ServerThread,
) -> None:
    client = make_client(make_config(), env={"K": SECRET})
    add_provider(
        client,
        {
            "id": "remote",
            "adapter_type": "openai_compatible",
            "config": {"base_url": f"{openai_server.url}/v1", "api_key_ref": "env:K"},
        },
    )
    result = client.post(
        "/api/v1/deployments/verify", json={"provider_id": "remote", "model": "remote-small"}
    ).json()["data"]
    checks = {c["capability"]: c["result"] for c in result["checks"]}
    assert checks["chat"] == "passed" and checks["streaming"] == "passed"
    claims = {
        c["capability"]: c["evidence"] for c in result["deployment"]["capabilities"]["claims"]
    }
    assert claims["chat"] == "verified" and claims["streaming"] == "verified"


# ---------------------------------------------------------------- 11: persistence across restarts


def test_metadata_survives_restart(
    make_config: Callable[..., AppConfig], make_client: Callable[..., TestClient]
) -> None:
    config = make_config()
    first = make_client(config)
    assert (
        first.post(
            "/api/v1/models",
            json={
                "id": "my-org/custom-7b",
                "display_name": "Custom 7B",
                "family": "custom",
                "context_window": 8192,
                "capabilities": ["chat", "tool_calling"],
                "license": {"name": "Proprietary", "commercial_use": "restricted"},
            },
        ).status_code
        == 201
    )
    add_provider(first, {"id": "runtime-mock", "adapter_type": "mock"})
    first.patch("/api/v1/providers/mock", json={"enabled": False})
    conv = chat(first, provider_id="runtime-mock", persist=True).json()["data"]["conversation_id"]
    first.__exit__(None, None, None)

    second = make_client(config)  # same SQLite file, new process state
    model = second.get("/api/v1/models/my-org/custom-7b").json()["data"]["definition"]
    assert model["context_window"] == 8192 and model["license"]["commercial_use"] == "restricted"
    assert {c["capability"] for c in model["capabilities"]["claims"]} == {"chat", "tool_calling"}
    providers = {p["id"]: p for p in second.get("/api/v1/providers").json()["data"]}
    assert (
        providers["runtime-mock"]["origin"] == "runtime" and providers["mock"]["enabled"] is False
    )
    assert len(second.get(f"/api/v1/conversations/{conv}").json()["data"]["messages"]) == 2


# ---------------------------------------------------------------- 12: runtime config validation


def test_runtime_configuration_changes_are_validated(
    make_config: Callable[..., AppConfig], make_client: Callable[..., TestClient]
) -> None:
    client = make_client(make_config())
    bad = [
        {"id": "x", "adapter_type": "does-not-exist"},
        {"id": "x", "adapter_type": "ollama", "config": {"base_url": "http://169.254.169.254/"}},
        {"id": "x", "adapter_type": "ollama", "config": {"base_url": "ftp://host"}},
        {"id": "x", "adapter_type": "ollama", "config": {"unknown_field": 1}},
        {
            "id": "x",
            "adapter_type": "openai_compatible",
            "config": {
                "base_url": "http://127.0.0.1:1/v1",
                "extra_headers": {"Authorization": "Bearer leak"},
            },
        },
        {
            "id": "x",
            "adapter_type": "openai_compatible",
            "config": {"base_url": "http://127.0.0.1:1/v1", "api_key_ref": "sk-literal-key"},
        },
        {"id": "Bad Id", "adapter_type": "mock"},
    ]
    for body in bad:
        response = client.post("/api/v1/providers", json=body)
        assert response.status_code == 422, (body, response.text)
        assert response.json()["error"]["code"] in ("validation_failed", "egress_denied")
    assert (
        client.post("/api/v1/providers", json={"id": "mock", "adapter_type": "mock"}).status_code
        == 409
    )
    assert (
        client.patch("/api/v1/providers/mock", json={"config": {"token_delay_ms": 0}}).status_code
        == 422
    )
    for settings in (
        {"default_provider_id": "ghost"},
        {"routing": {"mode": "warp"}},
        {"routing": {"fallback_provider_ids": ["mock"]}},
        {"request_timeout_s": -1},
    ):
        assert client.patch("/api/v1/settings", json=settings).status_code == 422, settings
    audit = client.get("/api/v1/audit").json()["data"]
    assert audit == []  # rejected changes leave no audit trail of success


# ---------------------------------------------------------------- 13: credentials


def test_credentials_never_appear_in_api_responses(
    make_config: Callable[..., AppConfig],
    make_client: Callable[..., TestClient],
    openai_server: ServerThread,
) -> None:
    client = make_client(make_config(), env={"REMOTE_LLM_API_KEY": SECRET})
    created = client.post(
        "/api/v1/providers",
        json={
            "id": "remote",
            "adapter_type": "openai_compatible",
            "config": {
                "base_url": f"{openai_server.url}/v1",
                "api_key_ref": "env:REMOTE_LLM_API_KEY",
            },
        },
    )
    responses = [created]
    for path in [
        "/api/v1/providers",
        "/api/v1/providers/remote",
        "/api/v1/adapters",
        "/api/v1/settings",
        "/api/v1/secrets",
        "/api/v1/audit",
        "/api/v1/deployments",
        "/api/v1/health/providers?refresh=true",
        "/api/v1/system",
        "/openapi.json",
        "/api/v1/requests/recent",
        "/api/v1/metrics/telemetry",
    ]:
        responses.append(client.get(path))
    responses.append(client.post("/api/v1/providers/test", json={"provider_id": "remote"}))
    responses.append(chat(client, provider_id="remote", model="remote-small"))
    responses.append(chat(client, provider_id="remote", model="no-such-model"))
    for r in responses:
        assert r.status_code < 500, r.text
        assert SECRET not in r.text, r.request.url
    secrets = client.get("/api/v1/secrets").json()["data"]
    assert {"reference": "env:REMOTE_LLM_API_KEY", "available": True} in secrets
    view = client.get("/api/v1/providers/remote").json()["data"]
    assert view["secret_references"] == [{"reference": "env:REMOTE_LLM_API_KEY", "available": True}]


# ---------------------------------------------------------------- 14: benchmarks


def test_benchmarks_run_and_export(
    make_config: Callable[..., AppConfig],
    make_client: Callable[..., TestClient],
    ollama_server: ServerThread,
) -> None:
    client = make_client(make_config())
    add_provider(
        client,
        {
            "id": "ollama",
            "adapter_type": "ollama",
            "compute_environment_id": "local",
            "config": {"base_url": ollama_server.url},
        },
    )
    response = client.post(
        "/api/v1/benchmarks?wait=true",
        json={
            "name": "mock vs ollama",
            "dataset_id": "smoke",
            "iterations": 2,
            "warmup_runs": 1,
            "targets": [
                {"provider_id": "mock", "model": "mock-chat"},
                {"provider_id": "ollama", "model": "qwen2.5:0.5b"},
            ],
        },
    )
    assert response.status_code == 202
    run = response.json()["data"]
    assert run["status"] == "completed", run
    assert run["dataset_version"] == "1.0.0"
    # 1 cold + 2 iterations x 3 prompts per target; warm-up requests are not recorded
    assert len(run["samples"]) == 2 * (1 + 2 * 3)
    assert {s["phase"] for s in run["samples"]} == {"cold", "warm"}
    summaries = {s["provider_id"]: s for s in run["summaries"]}
    assert summaries["ollama"]["environment"]["quantization"] == "Q4_K_M"
    assert summaries["mock"]["warm_p50_total_ms"] is not None
    assert run["comparable"] is False  # different quantization: flagged, not hidden
    assert any("quantization" in n for n in run["comparability_notes"])

    csv_export = client.get(f"/api/v1/benchmarks/{run['id']}/export?format=csv")
    assert csv_export.status_code == 200 and csv_export.headers["content-type"].startswith(
        "text/csv"
    )
    lines = csv_export.text.strip().splitlines()
    assert lines[0].startswith("run_id,run_name,dataset_id,dataset_version,provider_id")
    assert len(lines) == 1 + len(run["samples"])
    json_export = client.get(f"/api/v1/benchmarks/{run['id']}/export?format=json").json()
    assert json_export["format"] == "omf.benchmark.v1" and json_export["run"]["id"] == run["id"]
    stored = client.post(f"/api/v1/benchmarks/{run['id']}/export-to-storage?format=csv").json()[
        "data"
    ]
    assert stored["key"] == f"benchmarks/{run['id']}.csv"
    downloaded = client.get("/api/v1/storage/local/artifact/content", params={"key": stored["key"]})
    assert downloaded.text == csv_export.text


# ---------------------------------------------------------------- security middleware


def test_api_key_authentication_and_roles(
    make_config: Callable[..., AppConfig], make_client: Callable[..., TestClient]
) -> None:
    env = {
        "ADMIN_TOKEN": "admin-token-0123456789abcdef",
        "USER_TOKEN": "user-token-0123456789abcdef",
    }
    config = make_config(
        auth={
            "mode": "api_key",
            "tokens": [
                {"name": "admin", "role": "admin", "token_ref": "env:ADMIN_TOKEN"},
                {"name": "app", "role": "user", "token_ref": "env:USER_TOKEN"},
            ],
        }
    )
    client = make_client(config, env=env)
    assert client.get("/health/live").status_code == 200  # probes stay open
    unauth = chat(client)
    assert unauth.status_code == 401 and unauth.json()["error"]["code"] == "authentication_required"
    assert (
        client.get("/api/v1/providers", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )
    user = {"Authorization": f"Bearer {env['USER_TOKEN']}"}
    admin = {"Authorization": f"Bearer {env['ADMIN_TOKEN']}"}
    assert (
        client.post(
            "/api/v1/chat/completions",
            headers=user,
            json={"messages": [{"role": "user", "content": "x"}]},
        ).status_code
        == 200
    )
    forbidden = client.post(
        "/api/v1/providers", headers=user, json={"id": "m2", "adapter_type": "mock"}
    )
    assert forbidden.status_code == 403 and forbidden.json()["error"]["code"] == "permission_denied"
    assert client.patch("/api/v1/settings", headers=user, json={}).status_code == 403
    assert (
        client.post(
            "/api/v1/providers", headers=admin, json={"id": "m2", "adapter_type": "mock"}
        ).status_code
        == 201
    )
    audit = client.get("/api/v1/audit", headers=admin).json()["data"]
    assert audit[0]["actor"] == "admin" and audit[0]["action"] == "provider.create"


def test_request_size_limit_and_security_headers(
    make_config: Callable[..., AppConfig], make_client: Callable[..., TestClient]
) -> None:
    client = make_client(make_config(server={"max_request_bytes": 2048}))
    big = chat(client, messages=[{"role": "user", "content": "x" * 5000}])
    assert big.status_code == 413 and big.json()["error"]["code"] == "request_too_large"
    ok = chat(client)
    assert (
        ok.headers["x-content-type-options"] == "nosniff"
        and ok.headers["x-frame-options"] == "DENY"
    )


def test_validation_errors_do_not_echo_input(
    make_config: Callable[..., AppConfig], make_client: Callable[..., TestClient]
) -> None:
    client = make_client(make_config())
    response = client.post(
        "/api/v1/chat/completions",
        json={"messages": [{"role": "hacker", "content": "my secret prompt"}]},
    )
    assert response.status_code == 422
    assert "my secret prompt" not in response.text


def test_cors_allowlist(
    make_config: Callable[..., AppConfig], make_client: Callable[..., TestClient]
) -> None:
    client = make_client(make_config(server={"cors_allow_origins": ["http://allowed.example"]}))
    allowed = client.options(
        "/api/v1/providers",
        headers={"Origin": "http://allowed.example", "Access-Control-Request-Method": "GET"},
    )
    assert allowed.headers.get("access-control-allow-origin") == "http://allowed.example"
    denied = client.options(
        "/api/v1/providers",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in denied.headers


def test_artifact_upload_validation(
    make_config: Callable[..., AppConfig], make_client: Callable[..., TestClient]
) -> None:
    client = make_client(make_config())
    ok = client.post(
        "/api/v1/storage/local/artifacts",
        data={"key": "experiments/a.json"},
        files={"file": ("a.json", b'{"x":1}', "application/json")},
    )
    assert ok.status_code == 201 and ok.json()["data"]["size_bytes"] == 7
    bad_key = client.post(
        "/api/v1/storage/local/artifacts",
        data={"key": "../../etc/passwd"},
        files={"file": ("a", b"x", "text/plain")},
    )
    assert bad_key.status_code == 422
    bad_type = client.post(
        "/api/v1/storage/local/artifacts",
        data={"key": "x.html"},
        files={"file": ("x.html", b"<script>", "text/html")},
    )
    assert bad_type.status_code == 422
    verify = client.post(
        "/api/v1/storage/local/artifact/verify", params={"key": "experiments/a.json"}
    )
    assert verify.json()["data"]["ok"] is True
