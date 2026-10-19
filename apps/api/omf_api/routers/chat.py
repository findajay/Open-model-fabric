"""Chat completions (JSON and Server-Sent Events) and cancellation."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from omf.core.domain.models import InferenceResponse
from omf.core.services.inference import ChatCommand
from omf_api.deps import ContainerDep
from omf_api.schemas import CancelResult, ChatRequest, Envelope, ok
from omf_api.security import UserDep
from omf_api.sse import sse_stream

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


def _command(body: ChatRequest, request: Request) -> ChatCommand:
    return ChatCommand(
        messages=body.domain_messages(),
        provider_id=body.provider_id,
        model=body.model,
        params=body.params(),
        conversation_id=body.conversation_id,
        persist=body.persist,
        sensitivity=body.sensitivity,
        required_capabilities=set(body.required_capabilities),
        request_id=request.state.request_id,
    )


@router.post("/completions", summary="Non-streaming chat completion")
async def chat_completion(
    body: ChatRequest, request: Request, c: ContainerDep, _p: UserDep
) -> Envelope[InferenceResponse]:
    return ok(await c.inference.complete(_command(body, request)))


@router.post(
    "/stream",
    summary="Streaming chat completion (Server-Sent Events)",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "Events: message.started, message.delta, usage.reported, "
            "message.completed, request.failed",
        }
    },
)
async def chat_stream(
    body: ChatRequest, request: Request, c: ContainerDep, _p: UserDep
) -> StreamingResponse:
    cmd = _command(body, request)
    # Routing/validation errors are returned as normal HTTP errors before the stream starts;
    # failures after that arrive as a terminal `request.failed` event.
    await c.inference.preflight(cmd, streaming=True)
    events = c.inference.stream(cmd)
    return StreamingResponse(
        sse_stream(
            events,
            receive=request.receive,
            on_disconnect=lambda: c.inference.cancel(cmd.request_id),
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/requests/{request_id}/cancel", summary="Cancel an in-flight request")
async def cancel_request(request_id: str, c: ContainerDep, _p: UserDep) -> Envelope[CancelResult]:
    return ok(CancelResult(request_id=request_id, cancelled=await c.inference.cancel(request_id)))


@router.get("/requests", summary="Ids of in-flight requests")
async def inflight(c: ContainerDep, _p: UserDep) -> Envelope[list[str]]:
    return ok(c.inference.inflight())
