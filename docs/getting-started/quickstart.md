# Quickstart

To try every feature step by step, see the [laptop test guide](laptop-test-guide.md).

Everything below runs on a laptop **without a GPU, model weights, network access or a
cloud account**, using the mock inference provider. Real models are one configuration
change away.

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | 3.11+ | 3.11 and 3.13 are tested |
| [uv](https://docs.astral.sh/uv/) | 0.11+ | Python dependency manager (`uv.lock` is committed) |
| Node.js | 20+ | 22 is used in CI |
| pnpm | 10.18 | `corepack enable` installs the pinned version |

## 1. Install and start (one command)

```bash
git clone https://github.com/findajay/Open-model-fabric.git
cd Open-model-fabric
make install   # uv sync + pnpm install, from lockfiles
make dev       # API on :8080 (development profile) + web UI on :5173
```

Open <http://localhost:5173>. The API docs (OpenAPI/Swagger) are at
<http://127.0.0.1:8080/docs>.

The development profile (`configs/development.yaml`) uses the mock provider, local
compute, local-filesystem artifacts under `./var/artifacts` and SQLite at `./var/omf.db`.
Authentication is off **only** because the API binds to `127.0.0.1`; the configuration
validator refuses `auth.mode: none` on any other address.

## 2. Chat

Go to **AI Playground**, keep *Provider: Auto (routing policy)* or pick `mock`, and send
a message. You will see the streamed reply, time to first token, total latency, tokens/s
and token usage. Conversations are saved in the left sidebar.

From the command line:

```bash
curl -N http://127.0.0.1:8080/api/v1/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Hello"}]}'
```

## 3. Use a real model with Ollama

```bash
ollama serve &                   # https://ollama.com
ollama pull qwen2.5:0.5b         # ~400 MB, runs on CPU
```

Then either:

* **Without restarting:** *Providers → Add provider*, adapter **Ollama**, id `ollama`,
  base URL `http://127.0.0.1:11434`, *Test connectivity*, *Save*. Select it in the
  Playground.
* **Or start with the Ollama profile:** `OMF_PROFILE=local-ollama make dev-api`
  (plus `make dev-web`).

Capabilities shown for Ollama models come from Ollama's own `/api/show` metadata
(`provider_reported`). Vision upload appears only for models that report `vision`.

## 4. Connect an OpenAI-compatible server (vLLM, llama.cpp, TGI, LM Studio, …)

Put the server's API key (if any) in the **backend's** environment, never in the browser:

```bash
export REMOTE_LLM_API_KEY=...    # before starting the API
```

*Providers → Add provider*, adapter **OpenAI-compatible**, base URL including `/v1`
(e.g. `http://gpu-box:8000/v1`), API key reference `env:REMOTE_LLM_API_KEY`, locality
`external` if the server is outside your trust boundary (sensitive prompts are then
never routed to it).

## 5. See provider switching end to end

```bash
make demo   # same request -> mock, Ollama, OpenAI-compatible (emulators unless URLs given)
make e2e    # the same flow in a real browser with Playwright
```

`make demo` uses local **protocol emulators** for Ollama and the OpenAI-compatible
server unless you pass real URLs (`python scripts/demo_provider_switch.py --help`). The
emulators speak the real wire protocols but do not run a model.

## 6. Benchmarks

**Benchmark Lab** → choose the `smoke@1.0.0` dataset and one or more deployments → *Run*.
Results separate the cold first request from warm requests, flag incomparable targets
(different hardware, quantization or token-count source), and export to CSV/JSON. The
Colab notebook (`notebooks/colab_model_lab.ipynb`) exports the same CSV columns.

## Next steps

* [Troubleshooting](troubleshooting.md)
* [Deployment](../deployment/README.md) — Docker, Compose, Kubernetes, GPU inference
* [Architecture overview](../architecture/overview.md) and [ADRs](../architecture/adr/README.md)
* [Writing an adapter](../providers/developing-adapters.md)
