"""Ollama *protocol* emulator for tests and offline demos.

It implements the subset of the Ollama REST API that Open Model Fabric uses — ``GET
/api/version``, ``GET /api/tags``, ``POST /api/show``, ``POST /api/chat`` (JSON and
NDJSON streaming) — with response shapes taken from the Ollama API documentation.

It does NOT run a language model: replies are deterministic text. It exists so that the
adapter and the end-to-end provider-switching demo can be exercised where a real Ollama
cannot be installed (e.g. CI, sandboxes). Point the same configuration at a real Ollama
(``OLLAMA_BASE_URL``) to run against actual models.

Run standalone: ``python -m tests.emulators.ollama_emulator --port 11435``
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from tests.emulators._disconnect import until_disconnect

MODELS: dict[str, dict[str, Any]] = {
    "qwen2.5:0.5b": {
        "details": {
            "format": "gguf",
            "family": "qwen2",
            "parameter_size": "494.03M",
            "quantization_level": "Q4_K_M",
        },
        "capabilities": ["completion", "tools"],
        "model_info": {"general.architecture": "qwen2", "qwen2.context_length": 32768},
    },
    "qwen2.5vl:3b": {
        "details": {
            "format": "gguf",
            "family": "qwen25vl",
            "parameter_size": "3.8B",
            "quantization_level": "Q4_K_M",
        },
        "capabilities": ["completion", "vision"],
        "model_info": {"general.architecture": "qwen25vl", "qwen25vl.context_length": 128000},
    },
    "nomic-embed-text:latest": {
        "details": {
            "format": "gguf",
            "family": "nomic-bert",
            "parameter_size": "137M",
            "quantization_level": "F16",
        },
        "capabilities": ["embedding"],
        "model_info": {"general.architecture": "nomic-bert", "nomic-bert.context_length": 2048},
    },
}


@dataclass
class EmulatorState:
    token_delay_s: float = 0.01
    load_delay_s: float = 0.0
    fail_with_status: int | None = None
    report_capabilities: bool = True
    requests: list[dict[str, Any]] = field(default_factory=list)
    completed_streams: int = 0
    aborted_streams: int = 0


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _reply(model: str, body: dict[str, Any]) -> str:
    messages = body.get("messages", [])
    last = next((m for m in reversed(messages) if m.get("role") == "user"), {})
    prompt = str(last.get("content", ""))
    images = len(last.get("images") or [])
    if body.get("format") == "json":
        return json.dumps({"engine": "ollama-emulator", "model": model, "echo": prompt[:100]})
    suffix = f" I also received {images} image(s)." if images else ""
    return f"Ollama emulator ({model}) received: {prompt}.{suffix} This is not a real model."


def _words(text: str) -> list[str]:
    parts = text.split(" ")
    return [p + (" " if i < len(parts) - 1 else "") for i, p in enumerate(parts)]


def create_app(state: EmulatorState | None = None) -> Starlette:
    st = state or EmulatorState()

    async def version(_: Request) -> Response:
        if st.fail_with_status:
            return JSONResponse({"error": "emulated failure"}, status_code=st.fail_with_status)
        return JSONResponse({"version": "0.6.8-emulator"})

    async def tags(_: Request) -> Response:
        if st.fail_with_status:
            return JSONResponse({"error": "emulated failure"}, status_code=st.fail_with_status)
        return JSONResponse(
            {
                "models": [
                    {
                        "name": name,
                        "model": name,
                        "modified_at": _now(),
                        "size": 397821319,
                        "digest": "0" * 64,
                        "details": m["details"],
                    }
                    for name, m in MODELS.items()
                ]
            }
        )

    async def show(request: Request) -> Response:
        body = await request.json()
        name = body.get("model") or body.get("name")
        if name not in MODELS:
            return JSONResponse({"error": f"model '{name}' not found"}, status_code=404)
        m = MODELS[name]
        payload: dict[str, Any] = {
            "details": m["details"],
            "model_info": m["model_info"],
            "modified_at": _now(),
        }
        if st.report_capabilities:
            payload["capabilities"] = m["capabilities"]
        return JSONResponse(payload)

    async def chat(request: Request) -> Response:
        body = await request.json()
        st.requests.append(body)
        if st.fail_with_status:
            return JSONResponse({"error": "emulated failure"}, status_code=st.fail_with_status)
        model = body.get("model")
        if model not in MODELS:
            return JSONResponse(
                {"error": f"model '{model}' not found, try pulling it first"}, status_code=404
            )
        if "completion" not in MODELS[model]["capabilities"]:
            return JSONResponse({"error": f'"{model}" does not support chat'}, status_code=400)
        words = _words(_reply(model, body))
        limit = (body.get("options") or {}).get("num_predict")
        if limit:
            words = words[: int(limit)]
        prompt_tokens = sum(
            len(str(m.get("content", "")).split()) for m in body.get("messages", [])
        )
        final = {
            "model": model,
            "created_at": _now(),
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "done_reason": "length" if limit and len(words) >= int(limit) else "stop",
            "total_duration": 1,
            "load_duration": 1,
            "prompt_eval_count": prompt_tokens,
            "eval_count": len(words),
            "eval_duration": 1,
        }
        if not body.get("stream", True):
            await asyncio.sleep(st.load_delay_s + st.token_delay_s * len(words))
            return JSONResponse(
                {**final, "message": {"role": "assistant", "content": "".join(words)}}
            )

        async def gen() -> Any:
            try:
                await asyncio.sleep(st.load_delay_s)
                for w in words:
                    yield (
                        json.dumps(
                            {
                                "model": model,
                                "created_at": _now(),
                                "message": {"role": "assistant", "content": w},
                                "done": False,
                            }
                        )
                        + "\n"
                    )
                    await asyncio.sleep(st.token_delay_s)
                yield json.dumps(final) + "\n"
                st.completed_streams += 1
            except (asyncio.CancelledError, GeneratorExit):
                st.aborted_streams += 1
                raise

        def aborted() -> None:
            st.aborted_streams += 1

        return StreamingResponse(
            until_disconnect(request, gen(), aborted), media_type="application/x-ndjson"
        )

    app = Starlette(
        routes=[
            Route("/api/version", version),
            Route("/api/tags", tags),
            Route("/api/show", show, methods=["POST"]),
            Route("/api/chat", chat, methods=["POST"]),
        ]
    )
    app.state.emulator = st
    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11435)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
