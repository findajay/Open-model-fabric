"""OpenAI audio endpoints for the protocol emulator (shapes from the OpenAI API spec).

``POST /v1/audio/transcriptions`` (multipart: file, model, response_format = json |
verbose_json | text) and ``POST /v1/audio/speech`` (JSON: model, input, voice,
response_format, speed). No speech model runs: transcripts describe the upload, and
speech is a short tone (WAV) or a stub with the right container signature.
"""

from __future__ import annotations

import hashlib
import io
import math
import struct
import wave
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

TRANSCRIPTION_MODELS = {"whisper-small"}
SPEECH_MODELS = {"kokoro"}
SPEECH_TYPES = {
    "mp3": ("audio/mpeg", b"ID3\x04\x00\x00\x00\x00\x00\x00"),
    "opus": ("audio/ogg", b"OggS\x00\x02"),
    "flac": ("audio/flac", b"fLaC\x00\x00\x00\x22"),
}


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": "invalid_request_error", "code": status}},
        status_code=status,
    )


def tone_wav(seconds: float = 0.25, rate: int = 16_000) -> bytes:
    frames = b"".join(
        struct.pack("<h", int(8000 * math.sin(2 * math.pi * 330 * i / rate)))
        for i in range(int(rate * seconds))
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(frames)
    return buffer.getvalue()


async def transcriptions(request: Request, st: Any) -> Response:
    form = await request.form()
    upload = form.get("file")
    fields = {k: v for k, v in form.items() if isinstance(v, str)}
    st.requests.append({"path": "/v1/audio/transcriptions", **fields})
    model = fields.get("model")
    if model not in TRANSCRIPTION_MODELS:
        return _error(400, f"The model {model} does not support Transcriptions API")
    if upload is None or isinstance(upload, str):
        return _error(400, "file is required")
    data = await upload.read()
    digest = hashlib.sha256(data).hexdigest()[:12]
    text = f"emulated transcript of {upload.filename} ({len(data)} bytes, {digest})"
    fmt = fields.get("response_format", "json")
    if fmt == "text":
        return PlainTextResponse(text)
    if fmt == "verbose_json":
        return JSONResponse(
            {
                "task": "transcribe",
                "language": fields.get("language") or "english",
                "duration": 1.5,
                "text": text,
                "segments": [],
            }
        )
    return JSONResponse({"text": text})


async def speech(request: Request, st: Any) -> Response:
    body = await request.json()
    st.requests.append({"path": "/v1/audio/speech", **body})
    if body.get("model") not in SPEECH_MODELS:
        return _error(400, f"The model {body.get('model')} does not support Speech API")
    if not body.get("input") or not body.get("voice"):
        return _error(400, "input and voice are required")
    fmt = body.get("response_format", "mp3")
    if fmt == "wav":
        return Response(tone_wav(), media_type="audio/wav")
    if fmt not in SPEECH_TYPES:
        return _error(400, f"unsupported response_format {fmt!r}")
    media_type, header = SPEECH_TYPES[fmt]
    return Response(header + b"\x00" * 64, media_type=media_type)
