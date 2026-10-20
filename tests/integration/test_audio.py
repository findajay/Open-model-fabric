"""Speech-to-text and text-to-speech: mock adapter, OpenAI-compatible wire format (against
the protocol emulator; no speech model runs), routing, validation and records."""

from __future__ import annotations

import io
import wave
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from omf.adapters.inference.ollama import OllamaConfig
from omf.adapters.inference.ollama import create as create_ollama
from omf.bootstrap.config import AppConfig
from omf.core.domain.errors import OperationNotSupported
from omf.core.domain.models import ProviderDefinition, SpeechRequest, TranscriptionRequest
from omf_api.routers.audio import sniff_audio
from tests.conftest import ServerThread, emulator_state
from tests.emulators._audio import tone_wav
from tests.integration.test_adapters import CTX

SECRET = "sk-test-0123456789abcdef-SECRET"
WAV = tone_wav(0.5)


def transcribe(
    client: TestClient,
    data: bytes = WAV,
    content_type: str = "audio/wav",
    **fields: str,
) -> httpx.Response:
    return client.post(
        "/api/v1/audio/transcriptions",
        files={"file": ("clip.wav", data, content_type)},
        data=fields,
    )


def add_remote(client: TestClient, server: ServerThread, **config: Any) -> None:
    provider = {
        "id": "remote",
        "adapter_type": "openai_compatible",
        "config": {
            "base_url": f"{server.url}/v1",
            "api_key_ref": "env:OMF_TEST_REMOTE_KEY",
            **config,
        },
        "models": [
            {"name": "whisper-small", "capabilities": ["audio_input"]},
            {"name": "kokoro", "capabilities": ["audio_output"]},
        ],
    }
    assert client.post("/api/v1/providers", json=provider).status_code == 201


# ---------------------------------------------------------------------- mock


def test_mock_transcription_and_speech(
    make_config: Callable[..., AppConfig], make_client: Callable[..., TestClient]
) -> None:
    client = make_client(make_config())
    response = transcribe(client, language="en")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["model"] == "mock-whisper" and data["route"]["policy"] == "default"
    assert f"{len(WAV)} bytes" in data["text"] and data["text"].endswith("language=en")
    assert data["duration_s"] == 0.5 and data["language"] == "en"

    speech = client.post("/api/v1/audio/speech", json={"input": "hello there world"})
    assert speech.status_code == 200
    assert speech.headers["content-type"] == "audio/wav"
    assert speech.headers["x-omf-model"] == "mock-tts"
    with wave.open(io.BytesIO(speech.content)) as wav:
        assert wav.getframerate() == 16_000 and wav.getnframes() > 0

    mp3 = client.post("/api/v1/audio/speech", json={"input": "hi", "format": "mp3"})
    assert mp3.status_code == 422 and "only produces wav" in mp3.json()["error"]["message"]

    records = client.get("/api/v1/requests/recent").json()["data"]
    assert {r["operation"] for r in records} == {"transcription", "speech"}
    stats = client.get("/api/v1/metrics/summary?operation=transcription").json()["data"]
    assert stats["window_requests"] == 1


@pytest.mark.parametrize(
    ("data", "content_type", "fragment"),
    [
        (WAV, "application/pdf", "not an accepted audio type"),
        (b"ID3\x04" + b"\x00" * 64, "audio/wav", "does not match"),  # mp3 labeled as wav
        (b"", "audio/wav", "empty"),
        (b"not audio at all", "audio/ogg", "does not match"),
    ],
)
def test_transcription_upload_validation(
    make_config: Callable[..., AppConfig],
    make_client: Callable[..., TestClient],
    data: bytes,
    content_type: str,
    fragment: str,
) -> None:
    response = transcribe(make_client(make_config()), data, content_type)
    assert response.status_code == 422
    assert fragment in response.json()["error"]["message"]


def test_audio_routes_on_capabilities(
    make_config: Callable[..., AppConfig], make_client: Callable[..., TestClient]
) -> None:
    client = make_client(make_config())
    refused = transcribe(client, model="mock-chat")
    assert refused.status_code == 422
    assert refused.json()["error"]["details"]["missing"] == ["audio_input"]
    speech = client.post("/api/v1/audio/speech", json={"input": "x", "model": "mock-whisper"})
    assert speech.json()["error"]["code"] == "capability_not_supported"
    assert client.post("/api/v1/audio/speech", json={"input": ""}).status_code == 422
    assert client.post("/api/v1/audio/speech", json={"input": "x", "speed": 9}).status_code == 422
    assert client.post("/api/v1/audio/speech", json={"input": "x" * 4097}).status_code == 422


def test_sniffing_recognises_common_containers() -> None:
    assert sniff_audio(WAV) == "wav"
    assert sniff_audio(b"OggS\x00") == "ogg"
    assert sniff_audio(b"fLaC\x00") == "flac"
    assert sniff_audio(b"\x1a\x45\xdf\xa3\x00") == "webm"
    assert sniff_audio(b"\x00\x00\x00\x20ftypM4A ") == "m4a"
    assert sniff_audio(b"\xff\xfb\x90\x00") == "mp3"
    assert sniff_audio(b"hello") is None


# ---------------------------------------------------------------------- OpenAI-compatible


def test_openai_compatible_audio(
    make_config: Callable[..., AppConfig],
    make_client: Callable[..., TestClient],
    openai_server: ServerThread,
) -> None:
    client = make_client(make_config(), env={"OMF_TEST_REMOTE_KEY": SECRET})
    add_remote(client, openai_server)
    data = transcribe(client, provider_id="remote", language="de", prompt="names").json()["data"]
    assert data["text"].startswith("emulated transcript of clip.wav")
    assert data["model"] == "whisper-small" and data["duration_s"] is None  # plain json
    sent = emulator_state(openai_server).requests[-1]
    assert sent == {
        "path": "/v1/audio/transcriptions",
        "model": "whisper-small",
        "response_format": "json",
        "language": "de",
        "prompt": "names",
    }
    assert emulator_state(openai_server).seen_auth_headers[-1] == f"Bearer {SECRET}"

    for fmt, media in (("wav", "audio/wav"), ("mp3", "audio/mpeg"), ("opus", "audio/ogg")):
        speech = client.post(
            "/api/v1/audio/speech",
            json={"input": "hello", "voice": "af_bella", "format": fmt, "provider_id": "remote"},
        )
        assert speech.status_code == 200, speech.text
        assert speech.headers["content-type"] == media
    sent = emulator_state(openai_server).requests[-1]
    assert sent == {
        "path": "/v1/audio/speech",
        "model": "kokoro",
        "input": "hello",
        "voice": "af_bella",
        "response_format": "opus",
        "speed": 1.0,
    }


def test_openai_compatible_verbose_transcription(
    make_config: Callable[..., AppConfig],
    make_client: Callable[..., TestClient],
    openai_server: ServerThread,
) -> None:
    client = make_client(make_config(), env={"OMF_TEST_REMOTE_KEY": SECRET})
    add_remote(client, openai_server, transcription_format="verbose_json")
    data = transcribe(client, provider_id="remote").json()["data"]
    assert data["language"] == "english" and data["duration_s"] == 1.5


async def test_ollama_has_no_audio_api(ollama_server: ServerThread) -> None:
    adapter = create_ollama(
        ProviderDefinition(id="ollama", adapter_type="ollama"),
        OllamaConfig(base_url=ollama_server.url),
        CTX,
    )
    with pytest.raises(OperationNotSupported):
        await adapter.transcribe(
            TranscriptionRequest(
                request_id="r", model="m", audio=WAV, filename="a.wav", content_type="audio/wav"
            )
        )
    with pytest.raises(OperationNotSupported):
        await adapter.synthesize(SpeechRequest(request_id="r", model="m", input="x", voice="v"))
    await adapter.aclose()
