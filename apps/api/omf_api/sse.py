"""Server-Sent Events encoding with keep-alive comments.

Event stream contract (see ``omf.core.domain.events``)::

    id: 3
    event: message.delta
    data: {"request_id": "...", "index": 2, "delta": "Hello"}

A comment line (``: keep-alive``) is sent when no event was produced for
``keepalive_s`` seconds so proxies do not close idle connections during long
time-to-first-token waits (e.g. a model loading).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable

from starlette.types import Message

from omf.core.domain.events import StreamEvent


def encode(event: StreamEvent, event_id: int) -> bytes:
    payload = json.dumps(event.data.model_dump(mode="json"), separators=(",", ":"))
    return f"id: {event_id}\nevent: {event.event.value}\ndata: {payload}\n\n".encode()


async def watch_disconnect(
    receive: Callable[[], Awaitable[Message]], on_disconnect: Callable[[], Awaitable[object]]
) -> None:
    """Wait for ``http.disconnect`` and run ``on_disconnect``.

    Needed because servers implementing ASGI spec 2.4 (e.g. uvicorn) silently drop writes
    after a client disconnects, and Starlette no longer listens for the disconnect itself:
    without this, an abandoned stream would keep generating (and paying for) tokens.
    """
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            await on_disconnect()
            return


async def sse_stream(
    events: AsyncGenerator[StreamEvent, None],
    *,
    keepalive_s: float = 15.0,
    receive: Callable[[], Awaitable[Message]] | None = None,
    on_disconnect: Callable[[], Awaitable[object]] | None = None,
) -> AsyncIterator[bytes]:
    watcher = (
        asyncio.create_task(watch_disconnect(receive, on_disconnect))
        if receive is not None and on_disconnect is not None
        else None
    )
    try:
        async for chunk in _encode_stream(events, keepalive_s):
            yield chunk
    finally:
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher


async def _encode_stream(
    events: AsyncGenerator[StreamEvent, None], keepalive_s: float
) -> AsyncIterator[bytes]:
    event_id = 0
    async with contextlib.aclosing(events) as source:
        iterator = source.__aiter__()
        pending: asyncio.Future[StreamEvent] | None = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(iterator.__anext__())
                done, _ = await asyncio.wait({pending}, timeout=keepalive_s)
                if not done:
                    yield b": keep-alive\n\n"
                    continue
                try:
                    event = pending.result()
                except StopAsyncIteration:
                    return
                finally:
                    pending = None
                yield encode(event, event_id)
                event_id += 1
                if event.terminal:
                    return
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration, Exception):
                    await pending
