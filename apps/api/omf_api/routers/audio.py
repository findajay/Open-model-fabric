"""Speech-to-text and text-to-speech."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import Response
from pydantic import ConfigDict, Field

from omf.core.domain.base import DomainModel
from omf.core.domain.errors import ValidationFailed
from omf.core.domain.models import (
    AUDIO_INPUT_TYPES,
    SPEECH_FORMATS,
    Sensitivity,
    TranscriptionResponse,
)
from omf.core.services.inference import SpeechCommand, TranscribeCommand
from omf_api.deps import ContainerDep
from omf_api.schemas import Envelope, ok
from omf_api.security import UserDep

router = APIRouter(prefix="/api/v1/audio", tags=["audio"])

MAX_AUDIO_BYTES = 25 * 1024 * 1024
"""Per-file cap (OpenAI's limit). The server-wide ``max_request_bytes`` (10 MB by
default) applies first; raise it to accept longer recordings."""


def sniff_audio(data: bytes) -> str | None:
    """Container format from magic bytes, so a mislabeled upload is not forwarded."""
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:4] == b"OggS":
        return "ogg"
    if data[:4] == b"fLaC":
        return "flac"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if data[4:8] == b"ftyp":
        return "m4a"
    if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "mp3"
    return None


@router.post(
    "/transcriptions",
    summary="Speech-to-text with a model that declares audio_input",
)
async def create_transcription(
    request: Request,
    c: ContainerDep,
    _p: UserDep,
    file: Annotated[UploadFile, File(description="wav, mp3, m4a, ogg, webm or flac")],
    model: Annotated[str | None, Form(max_length=256)] = None,
    provider_id: Annotated[str | None, Form(max_length=64)] = None,
    language: Annotated[str | None, Form(pattern=r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$")] = None,
    prompt: Annotated[str | None, Form(max_length=2000)] = None,
    sensitivity: Annotated[Sensitivity, Form()] = Sensitivity.NORMAL,
) -> Envelope[TranscriptionResponse]:
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    declared = AUDIO_INPUT_TYPES.get(content_type)
    if declared is None:
        raise ValidationFailed(
            f"content type {content_type or 'missing'!r} is not an accepted audio type",
            details={"allowed": sorted(AUDIO_INPUT_TYPES)},
        )
    audio = await file.read(MAX_AUDIO_BYTES + 1)
    if len(audio) > MAX_AUDIO_BYTES:
        raise ValidationFailed("audio file too large", details={"limit": MAX_AUDIO_BYTES})
    if not audio:
        raise ValidationFailed("audio file is empty")
    actual = sniff_audio(audio)
    if actual != declared:
        raise ValidationFailed(
            f"file content does not match {content_type!r}",
            details={"declared": declared, "detected": actual},
        )
    return ok(
        await c.inference.transcribe(
            TranscribeCommand(
                audio=audio,
                filename=(file.filename or f"audio.{declared}")[-128:],
                content_type=content_type,
                provider_id=provider_id,
                model=model,
                language=language,
                prompt=prompt,
                sensitivity=sensitivity,
                request_id=request.state.request_id,
            )
        )
    )


class SpeechBody(DomainModel):
    model_config = ConfigDict(extra="forbid")

    input: str = Field(min_length=1, max_length=4096)
    voice: str = Field(default="alloy", min_length=1, max_length=64)
    format: Literal["mp3", "wav", "opus", "flac"] = "wav"
    speed: float = Field(default=1.0, ge=0.25, le=4.0)
    model: str | None = Field(default=None, max_length=256)
    provider_id: str | None = Field(default=None, max_length=64)
    sensitivity: Sensitivity = Sensitivity.NORMAL


@router.post(
    "/speech",
    summary="Text-to-speech with a model that declares audio_output (returns audio bytes)",
    response_class=Response,
    responses={
        200: {
            "content": {media: {} for media in sorted(set(SPEECH_FORMATS.values()))},
            "description": "The audio. Headers X-OMF-Provider and X-OMF-Model name the route.",
        }
    },
)
async def create_speech(
    body: SpeechBody, request: Request, c: ContainerDep, _p: UserDep
) -> Response:
    result, route = await c.inference.synthesize(
        SpeechCommand(
            input=body.input,
            voice=body.voice,
            format=body.format,
            speed=body.speed,
            provider_id=body.provider_id,
            model=body.model,
            sensitivity=body.sensitivity,
            request_id=request.state.request_id,
        )
    )
    return Response(
        content=result.audio,
        media_type=result.content_type,
        headers={"X-OMF-Provider": route.provider_id, "X-OMF-Model": route.model},
    )
