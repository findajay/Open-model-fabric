"""Normalised streaming events (the SSE contract).

Every inference engine streams differently (OpenAI SSE ``data:`` chunks, Ollama NDJSON,
in-process generators). Adapters normalise to ``GenerationChunk``; the inference service
turns those into this small, stable event vocabulary which the API emits verbatim as
Server-Sent Events. Exactly one terminal event (``message.completed`` or
``request.failed``) ends every stream.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from omf.core.domain.models import RouteDecision, Timings, TokenUsage


class StreamEventType(StrEnum):
    STARTED = "message.started"
    DELTA = "message.delta"
    USAGE = "usage.reported"
    COMPLETED = "message.completed"
    FAILED = "request.failed"


class MessageStarted(BaseModel):
    request_id: str
    conversation_id: str | None
    provider_id: str
    model: str
    model_id: str | None
    route: RouteDecision


class MessageDelta(BaseModel):
    request_id: str
    index: int
    delta: str


class UsageReported(BaseModel):
    request_id: str
    usage: TokenUsage


class MessageCompleted(BaseModel):
    request_id: str
    message_id: str
    conversation_id: str | None
    finish_reason: str | None
    timings: Timings
    usage: TokenUsage | None


class ErrorBody(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = {}


class RequestFailed(BaseModel):
    request_id: str
    error: ErrorBody
    partial: bool
    """True when some deltas were already delivered before the failure."""


class StreamEvent(BaseModel):
    event: StreamEventType
    data: MessageStarted | MessageDelta | UsageReported | MessageCompleted | RequestFailed

    @property
    def terminal(self) -> bool:
        return self.event in (StreamEventType.COMPLETED, StreamEventType.FAILED)
