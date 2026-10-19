"""OpenAI-compatible *protocol* emulator (the shape served by vLLM, llama.cpp, TGI, ...).

Implements ``GET /v1/models`` and ``POST /v1/chat/completions`` (JSON and SSE with
``stream_options.include_usage``), optionally requiring a bearer token. Replies are
deterministic text — no model runs. Used by tests and by the offline provider-switching
demo; point the same configuration at a real vLLM/llama.cpp server for real inference.

Run standalone: ``python -m tests.emulators.openai_emulator --port 8001 [--api-key KEY]``
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from tests.emulators._disconnect import until_disconnect

MODELS = {
    "Qwen/Qwen2.5-0.5B-Instruct": {"max_model_len": 32768},
    "remote-small": {"max_model_len": 4096},
}


@dataclass
class EmulatorState:
    api_key: str | None = None
    token_delay_s: float = 0.01
    first_token_delay_s: float = 0.0
    fail_with_status: int | None = None
    fail_mid_stream_after: int | None = None
    requests: list[dict[str, Any]] = field(default_factory=list)
    seen_auth_headers: list[str] = field(default_factory=list)
    completed_streams: int = 0
    aborted_streams: int = 0


def _error(status: int, message: str, kind: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": kind, "code": status}}, status_code=status
    )


def _text_of(content: Any) -> tuple[str, int]:
    if isinstance(content, str):
        return content, 0
    text = "".join(p.get("text", "") for p in content if p.get("type") == "text")
    images = sum(1 for p in content if p.get("type") == "image_url")
    return text, images


def create_app(state: EmulatorState | None = None) -> Starlette:
    st = state or EmulatorState()

    def check_auth(request: Request) -> Response | None:
        header = request.headers.get("authorization", "")
        st.seen_auth_headers.append(header)
        if st.api_key and header != f"Bearer {st.api_key}":
            return _error(401, "Incorrect API key provided.", "authentication_error")
        return None

    async def models(request: Request) -> Response:
        if (denied := check_auth(request)) is not None:
            return denied
        if st.fail_with_status:
            return _error(st.fail_with_status, "emulated failure", "server_error")
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {
                        "id": name,
                        "object": "model",
                        "created": 1700000000,
                        "owned_by": "emulator",
                        **meta,
                    }
                    for name, meta in MODELS.items()
                ],
            }
        )

    async def completions(request: Request) -> Response:
        if (denied := check_auth(request)) is not None:
            return denied
        body = await request.json()
        st.requests.append(body)
        if st.fail_with_status:
            return _error(st.fail_with_status, "emulated failure", "server_error")
        model = body.get("model")
        if model not in MODELS:
            return _error(404, f"The model `{model}` does not exist.", "NotFoundError")
        last = next((m for m in reversed(body.get("messages", [])) if m.get("role") == "user"), {})
        prompt, images = _text_of(last.get("content", ""))
        if body.get("response_format", {}).get("type") == "json_object":
            text = json.dumps({"engine": "openai-emulator", "model": model, "echo": prompt[:100]})
        else:
            extra = f" ({images} image(s) attached)" if images else ""
            text = (
                f"Remote OpenAI-compatible emulator ({model}) received: {prompt}{extra}. "
                "Not a real model."
            )
        parts = text.split(" ")
        words = [p + (" " if i < len(parts) - 1 else "") for i, p in enumerate(parts)]
        if body.get("max_tokens"):
            words = words[: int(body["max_tokens"])]
        finish = (
            "length" if body.get("max_tokens") and len(words) >= int(body["max_tokens"]) else "stop"
        )
        prompt_tokens = sum(
            len(_text_of(m.get("content", ""))[0].split()) for m in body.get("messages", [])
        )
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": len(words),
            "total_tokens": prompt_tokens + len(words),
        }
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        if not body.get("stream"):
            return JSONResponse(
                {
                    "id": cid,
                    "object": "chat.completion",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": finish,
                            "message": {"role": "assistant", "content": "".join(words)},
                        }
                    ],
                    "usage": usage,
                }
            )
        include_usage = (body.get("stream_options") or {}).get("include_usage", False)

        def chunk(delta: dict[str, Any], finish_reason: str | None = None) -> str:
            payload = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }
            return f"data: {json.dumps(payload)}\n\n"

        async def gen() -> Any:
            try:
                await asyncio.sleep(st.first_token_delay_s)
                yield chunk({"role": "assistant", "content": ""})
                for i, w in enumerate(words):
                    if st.fail_mid_stream_after is not None and i >= st.fail_mid_stream_after:
                        error = {"error": {"message": "emulated mid-stream failure"}}
                        yield f"data: {json.dumps(error)}\n\n"
                        return
                    yield chunk({"content": w})
                    await asyncio.sleep(st.token_delay_s)
                yield chunk({}, finish)
                if include_usage:
                    final = {
                        "id": cid,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [],
                        "usage": usage,
                    }
                    yield f"data: {json.dumps(final)}\n\n"
                yield "data: [DONE]\n\n"
                st.completed_streams += 1
            except (asyncio.CancelledError, GeneratorExit):
                st.aborted_streams += 1
                raise

        def aborted() -> None:
            st.aborted_streams += 1

        return StreamingResponse(
            until_disconnect(request, gen(), aborted), media_type="text/event-stream"
        )

    app = Starlette(
        routes=[
            Route("/v1/models", models),
            Route("/v1/chat/completions", completions, methods=["POST"]),
        ]
    )
    app.state.emulator = st
    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--api-key", default=None)
    args = parser.parse_args()
    uvicorn.run(
        create_app(EmulatorState(api_key=args.api_key)),
        host=args.host,
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
