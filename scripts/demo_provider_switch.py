"""Demonstrate provider switching from the command line.

Sends the *same* streaming chat request to the mock provider, an Ollama server and an
OpenAI-compatible server through one running Open Model Fabric API, changing only
``provider_id``. By default the Ollama and OpenAI-compatible servers are the protocol
emulators from tests/emulators (no model weights needed) and an API instance is started
in-process; pass real URLs to use actual engines:

    python scripts/demo_provider_switch.py
    python scripts/demo_provider_switch.py --ollama-url http://127.0.0.1:11434 \\
        --ollama-model qwen2.5:0.5b --openai-url http://127.0.0.1:8000/v1 \\
        --openai-model Qwen/Qwen2.5-0.5B-Instruct
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "apps" / "api"), str(ROOT)]

from tests.conftest import ServerThread  # noqa: E402
from tests.emulators import ollama_emulator, openai_emulator  # noqa: E402

from omf.bootstrap.config import load_config  # noqa: E402
from omf_api.app import create_app  # noqa: E402

DEMO_KEY = "sk-demo-0123456789abcdef"


def stream(api: str, provider_id: str, model: str, prompt: str) -> None:
    body = {
        "provider_id": provider_id,
        "model": model,
        "max_tokens": 128,
        "messages": [{"role": "user", "content": prompt}],
    }
    print(f"\n=== provider={provider_id} model={model}")
    with httpx.stream("POST", f"{api}/api/v1/chat/stream", json=body, timeout=120) as r:
        if r.status_code != 200:
            print(f"error {r.status_code}: {r.read().decode()}")
            return
        event = None
        for line in r.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:") and event:
                data = json.loads(line[5:])
                if event == "message.delta":
                    print(data["delta"], end="", flush=True)
                elif event == "message.completed":
                    t = data["timings"]
                    print(
                        f"\n--- ttft {t['ttft_ms']} ms, total {t['total_ms']} ms, "
                        f"{t['tokens_per_second']} tok/s, usage {data['usage']}"
                    )
                elif event == "request.failed":
                    print(f"\n--- failed: {data['error']['code']}: {data['error']['message']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ollama-url")
    parser.add_argument("--ollama-model", default="qwen2.5:0.5b")
    parser.add_argument("--openai-url")
    parser.add_argument("--openai-model", default="Qwen/Qwen2.5-0.5B-Instruct")
    parser.add_argument(
        "--openai-key-env",
        default="REMOTE_LLM_API_KEY",
        help="environment variable holding the remote server's API key",
    )
    parser.add_argument("--prompt", default="In one sentence: what does an inference gateway do?")
    args = parser.parse_args()

    servers = []
    if not args.ollama_url:
        servers.append(ServerThread(ollama_emulator.create_app()))
    if not args.openai_url:
        servers.append(
            ServerThread(
                openai_emulator.create_app(openai_emulator.EmulatorState(api_key=DEMO_KEY))
            )
        )
        os.environ[args.openai_key_env] = DEMO_KEY
    for s in servers:
        s.__enter__()
    ollama_url = args.ollama_url or servers[0].url
    openai_url = args.openai_url or f"{servers[-1].url}/v1"
    emulated = [
        n for n, real in (("ollama", args.ollama_url), ("openai", args.openai_url)) if not real
    ]
    if emulated:
        print(f"note: using protocol emulators (no real model) for: {', '.join(emulated)}")

    with tempfile.TemporaryDirectory() as data:
        config = load_config(
            ROOT / "configs" / "development.yaml", {**os.environ, "OMF_DATA_DIR": data}
        )
        with ServerThread(create_app(config, start_background=False)) as api:
            for body in (
                {"id": "ollama-demo", "adapter_type": "ollama", "config": {"base_url": ollama_url}},
                {
                    "id": "remote-demo",
                    "adapter_type": "openai_compatible",
                    "locality": "external",
                    "config": {"base_url": openai_url, "api_key_ref": f"env:{args.openai_key_env}"},
                },
            ):
                r = httpx.post(f"{api.url}/api/v1/providers", json=body, timeout=30)
                print(f"registered {body['id']}: HTTP {r.status_code}")
            stream(api.url, "mock", "mock-chat", args.prompt)
            stream(api.url, "ollama-demo", args.ollama_model, args.prompt)
            stream(api.url, "remote-demo", args.openai_model, args.prompt)
    for s in servers:
        s.__exit__(None, None, None)


if __name__ == "__main__":
    main()
