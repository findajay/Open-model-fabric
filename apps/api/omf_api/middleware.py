"""Pure-ASGI middleware (safe for streaming responses).

* Correlation id: read ``X-Request-ID`` (validated) or mint one; echo it on the response.
* Request size limit: reject on ``Content-Length`` and enforce while streaming the body,
  so chunked uploads cannot bypass it. Artifact upload routes get a larger limit.
* Security headers on every response.
* Access log line without query strings or bodies (no prompt content in logs).
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from omf_api.context import new_request_id, request_id_var

log = logging.getLogger("omf.access")
_UPLOAD_PATH = re.compile(r"^/api/v1/storage/[^/]+/artifacts$")

SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
]


class _TooLarge(Exception):
    pass


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, *, max_request_bytes: int, max_upload_bytes: int) -> None:
        self.app = app
        self.max_request_bytes = max_request_bytes
        self.max_upload_bytes = max_upload_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        request_id = new_request_id(headers.get("x-request-id"))
        token = request_id_var.set(request_id)
        scope.setdefault("state", {})["request_id"] = request_id
        limit = (
            self.max_upload_bytes if _UPLOAD_PATH.match(scope["path"]) else self.max_request_bytes
        )
        started = time.perf_counter()
        status_holder: dict[str, Any] = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                raw = list(message.get("headers", []))
                raw.append((b"x-request-id", request_id.encode()))
                existing = {k.lower() for k, _ in raw}
                raw.extend(h for h in SECURITY_HEADERS if h[0] not in existing)
                message["headers"] = raw
            await send(message)

        try:
            declared = headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > limit:
                await self._reject(send_wrapper, request_id, limit)
                return
            received = 0

            async def limited_receive() -> Message:
                nonlocal received
                message = await receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > limit:
                        raise _TooLarge
                return message

            try:
                await self.app(scope, limited_receive, send_wrapper)
            except _TooLarge:
                await self._reject(send_wrapper, request_id, limit)
        finally:
            log.info(
                "%s %s -> %s in %.1f ms",
                scope["method"],
                scope["path"],
                status_holder["status"],
                (time.perf_counter() - started) * 1000,
            )
            request_id_var.reset(token)

    @staticmethod
    async def _reject(send: Send, request_id: str, limit: int) -> None:
        body = json.dumps(
            {
                "error": {
                    "code": "request_too_large",
                    "message": f"request body exceeds {limit} bytes",
                    "request_id": request_id,
                    "retryable": False,
                    "details": {"limit_bytes": limit},
                }
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
