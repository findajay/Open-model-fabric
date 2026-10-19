"""Make emulator streams stop when the client disconnects, as real engines do.

uvicorn (ASGI spec 2.4) silently drops writes to a closed connection, so a plain
StreamingResponse generator would run to completion. Real servers (Ollama, vLLM) abort
generation on disconnect; this helper reproduces that so tests can observe it.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable

from starlette.requests import Request


async def until_disconnect(
    request: Request, source: AsyncIterator[str], on_abort: Callable[[], None]
) -> AsyncIterator[str]:
    disconnected = asyncio.Event()

    async def watch() -> None:
        while True:
            message = await request.receive()
            if message["type"] == "http.disconnect":
                disconnected.set()
                return

    watcher = asyncio.create_task(watch())
    try:
        async for chunk in source:
            if disconnected.is_set():
                on_abort()
                return
            yield chunk
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
