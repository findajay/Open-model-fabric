# Laptop test guide

A checklist for trying every feature on one laptop, in roughly 60–90 minutes. Each step
says what you should see. Steps marked **(needs …)** need extra software or an account.

| Feature | Test with | Verified before release |
|---|---|---|
| Chat, streaming, stop, conversations | mock (no GPU) | CI + e2e |
| Tool calling | mock, then Ollama | CI + e2e; real Ollama/llama.cpp in the `Real engines` workflow |
| Embeddings (+ Embeddings page) | mock, then Ollama | CI + e2e; real engines in CI |
| Audio: speech-to-text, text-to-speech | mock | CI + e2e (mock and emulator only, no real speech model) |
| API keys, expiry, rotation, rate limits | `configs/laptop-security.example.yaml` | CI; profile run end to end |
| SSO (OIDC) with Keycloak | `infrastructure/keycloak/omf-realm.json` | Run end to end against Keycloak 26.8.1 |
| Cancel across replicas | `configs/laptop-replicas.example.yaml` + PostgreSQL | Two API processes + PostgreSQL 17.11, end to end |
| Azure Blob / MinIO storage | Azurite / MinIO containers | CI against Azurite 3.37.0 and MinIO |
| Docker compute lifecycle | Docker on the laptop | CI against a real Docker Engine |
| Kubernetes and cloud compute | kind/minikube, or cloud accounts | **Fakes and moto only.** Your laptop run would be the first real one. |

## 0. Install and run the checks

You need Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 20+, pnpm 10 (`corepack
enable`) and, for the later sections, Docker.

```bash
git clone https://github.com/findajay/Open-model-fabric.git
cd Open-model-fabric
git checkout ccr-c519015f-o8omaf
make install
make check            # lint, types, ~260 Python tests, web tests, contract check
make e2e              # 6 browser tests (starts its own API, web and emulators)
```

Expect everything to pass. Tests marked as skipped need services you haven't started
(PostgreSQL, Docker, real engines).

## 1. The UI with the mock provider (no GPU)

```bash
make dev              # API :8080 + web :5173
```

Open <http://localhost:5173>.

1. **Playground:** send a message. It streams, and shows TTFT, tokens/s and usage.
   Press **Stop** mid-stream: the message shows "Stopped".
2. **Tool calling:** in the Playground, pick `mock` / `mock-chat` and turn on **Tools**.
   It is pre-filled with a `get_weather` tool. Send `Paris`.
   * Expect a **get_weather** card with `{"city": "Paris"}` and a *result* box.
   * Type `18 C, sunny` and press **Send tool results**.
   * Expect a reply quoting the result.
   * OMF never runs tools itself (ADR-0013).
3. **Embeddings page:** enter `the cat sat`, `a cat sat down` and `stock prices fell`,
   then compare.
   * The similarity matrix should show the first two texts much closer to each other
     than to the third.
   * The mock only hashes words; real semantic similarity needs a real model (section 2).
4. **Audio page:**
   * Upload any WAV, or record with the microphone (allow it in the browser), and
     transcribe.
     * `mock-whisper` describes the audio (bytes, hash, duration); it does not recognise
       speech.
   * Type text for speech: expect a short tone that plays.
5. **Providers / Models / Infrastructure / Benchmarks / Dashboard:** each page loads.
   * Dashboard metrics default to chat; embeddings and audio requests are labelled in
     *Recent requests*.

The same through the API:

```bash
curl -s localhost:8080/api/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Paris"}],
       "tools":[{"name":"get_weather","parameters":{"type":"object",
         "properties":{"city":{"type":"string"}},"required":["city"]}}]}' | python3 -m json.tool
curl -s localhost:8080/api/v1/embeddings -H 'Content-Type: application/json' \
  -d '{"input":["hello world","hello there"]}' | python3 -m json.tool | head -20
curl -s localhost:8080/api/v1/audio/speech -H 'Content-Type: application/json' \
  -d '{"input":"hello"}' -o hello.wav && file hello.wav
```

## 2. Real models with Ollama (CPU is fine)

```bash
ollama serve &
ollama pull qwen2.5:0.5b && ollama pull all-minilm
```

1. In **Providers → Add provider**, set the adapter to *Ollama* and the base URL to
   `http://127.0.0.1:11434`. Click *Test connectivity*, then *Save*.
2. **Models:** `qwen2.5:0.5b` shows chat/tool_calling and `all-minilm` shows embeddings.
   These are *provider_reported*: Ollama told OMF.
3. **Playground:** chat with `qwen2.5:0.5b`. Tools: ask *"What is the weather in Paris?
   Use the tool."*
   * Small models sometimes answer in text instead of calling the tool. That is the
     model, not OMF.
4. **Embeddings page** with `all-minilm`: paraphrases score clearly higher than unrelated
   text.
5. **Models → Verify** on each model: the checks pass and claims become *verified*.
6. Optional: run the real-engine test suite against your Ollama.

   ```bash
   OMF_TEST_OLLAMA_URL=http://127.0.0.1:11434 uv run pytest -m real_engine -v
   ```

   Tool tests may report `xfail` when the tiny model answers in text.

Ollama has no audio API, so audio requests to it return 501. For real speech, use an
OpenAI-compatible audio server, for example a whisper.cpp server for speech-to-text or
Kokoro-FastAPI for text-to-speech. Register it as `openai_compatible` and declare
`audio_input` or `audio_output` on its models. **No real speech server was tested before
release.**

## 3. API keys, expiry, rotation and rate limits

```bash
export OMF_ADMIN_TOKEN=admin-token-0123456789abcdef
export OMF_USER_TOKEN=user-token-0123456789abcdef
mkdir -p var/secrets && printf rotating-token-0000000000aa > var/secrets/rotating_token
OMF_CONFIG=configs/laptop-security.example.yaml make dev-api
```

In another terminal:

```bash
U="Authorization: Bearer $OMF_USER_TOKEN"; A="Authorization: Bearer $OMF_ADMIN_TOKEN"
curl -s -o /dev/null -w '%{http_code}\n' localhost:8080/api/v1/system          # 401
curl -s -o /dev/null -w '%{http_code}\n' -H "$U" localhost:8080/api/v1/audit   # 403 (user)
curl -s -o /dev/null -w '%{http_code}\n' -H "$A" localhost:8080/api/v1/audit   # 200 (admin)
# Rotation without restart: change the file, wait ~5 s (token_reload_s)
printf rotating-token-0000000000bb > var/secrets/rotating_token; sleep 6
curl -s -o /dev/null -w '%{http_code}\n' -H 'Authorization: Bearer rotating-token-0000000000aa' localhost:8080/api/v1/system  # 401
curl -s -o /dev/null -w '%{http_code}\n' -H 'Authorization: Bearer rotating-token-0000000000bb' localhost:8080/api/v1/system  # 200
# Rate limit: 5 inference calls per minute in this profile, so the 6th gets 429
for i in 1 2 3 4 5 6; do curl -s -o /dev/null -w '%{http_code} ' -H "$U" -H 'Content-Type: application/json' \
  localhost:8080/api/v1/chat/completions -d '{"messages":[{"role":"user","content":"hi"}]}'; done; echo
curl -si -H "$U" -H 'Content-Type: application/json' localhost:8080/api/v1/chat/completions \
  -d '{"messages":[{"role":"user","content":"hi"}]}' | grep -i retry-after
```

Expected: `200 200 200 200 200 429`, then a `Retry-After` header. The profile also
contains an expired entry that shares `OMF_USER_TOKEN`; the live entry still wins.

**Web UI with authentication:** run `make dev-web`, go to **Settings** and paste a token.
It is kept in session storage only.

## 4. SSO with Keycloak (needs Docker)

Start Keycloak with the ready-made realm:
* users `alice` / `alice-password` (role `omf-user`) and `admin` / `admin-password`
  (`omf-admin`);
* client `omf-cli`, whose tokens carry the `omf-api` audience.

```bash
docker run -d --name omf-keycloak -p 8180:8180 \
  -v "$PWD/infrastructure/keycloak:/opt/keycloak/data/import:ro" \
  -e KC_BOOTSTRAP_ADMIN_USERNAME=kcadmin -e KC_BOOTSTRAP_ADMIN_PASSWORD=kcadmin \
  keycloak/keycloak:26.8.1 start-dev --http-port=8180 --import-realm
# wait until this prints JSON:
curl -s localhost:8180/realms/omf/.well-known/openid-configuration | head -c 200; echo
```

With the API from section 3 still running (its profile already has the `oidc` block):

```bash
tok() { curl -s localhost:8180/realms/omf/protocol/openid-connect/token \
  -d grant_type=password -d client_id=omf-cli -d username=$1 -d password=$2 |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'; }
ALICE=$(tok alice alice-password); ADMIN=$(tok admin admin-password)
curl -s -H "Authorization: Bearer $ALICE" localhost:8080/api/v1/system | python3 -m json.tool | grep -E 'principal|role'
#   "principal": "sso:alice", "role": "user"
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $ALICE" localhost:8080/api/v1/audit  # 403
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $ADMIN" localhost:8080/api/v1/audit  # 200
curl -s -H "Authorization: Bearer ${ALICE%????}AAAA" localhost:8080/api/v1/system   # invalid token: InvalidSignatureError
```

OMF validates tokens. It does not show a login page; your app or a proxy such as
oauth2-proxy obtains the token. Access tokens last 15 minutes in this realm. Clean up
with `docker rm -f omf-keycloak`.

## 5. Cancel across replicas (needs Docker)

```bash
docker run -d --name omf-pg -p 5432:5432 -e POSTGRES_PASSWORD=omf postgres:17.11
export OMF_DATABASE_URL=postgresql+psycopg://postgres:omf@127.0.0.1:5432/postgres
export OMF_CONFIG=configs/laptop-replicas.example.yaml
uv run python -m omf_api --port 8080 &       # replica 1
uv run python -m omf_api --port 8081 &       # replica 2
# a long stream on replica 1:
curl -sN localhost:8080/api/v1/chat/stream -H 'Content-Type: application/json' \
  -H 'X-Request-ID: demo-1' -d '{"messages":[{"role":"user","content":"a long story"}]}' &
sleep 2
curl -s localhost:8081/api/v1/chat/requests                  # replica 2 lists "demo-1"
curl -s -X POST localhost:8081/api/v1/chat/requests/demo-1/cancel   # {"cancelled": true}
```

Within about half a second the stream on replica 1 ends with `event: request.failed`
and `request_cancelled`. In the pre-release run it closed 0.35 s after the cancel. Clean
up with `kill %1 %2; docker rm -f omf-pg`.

## 6. Storage backends (needs Docker)

**Azure Blob against Azurite** (Microsoft's emulator, which really checks Shared Key
signatures):

```bash
docker run -d --name omf-azurite -p 10000:10000 mcr.microsoft.com/azure-storage/azurite:3.37.0 \
  azurite-blob --blobHost 0.0.0.0 --blobPort 10000
uv run python scripts/azurite_container.py models      # the adapter never creates containers
export AZURE_STORAGE_KEY='Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=='   # Azurite's public dev key
```

Add this to the `storage:` list of the development profile, or a copy of it:

```yaml
  - id: azurite
    adapter: azure_blob
    config:
      account_name: devstoreaccount1
      container: models
      endpoint_url: http://127.0.0.1:10000/devstoreaccount1
      account_key_ref: env:AZURE_STORAGE_KEY
```

On **Infrastructure**, `azurite` shows *up*. Upload a file under *Browse artifacts*, or
use the API:

```bash
curl -s -F key=experiments/run1.json -F 'file=@pyproject.toml;type=text/plain' \
  localhost:8080/api/v1/storage/azurite/artifacts
curl -s localhost:8080/api/v1/storage/azurite/artifacts
```

The download is checksum-verified. To run the same contract suite as CI:

```bash
OMF_TEST_AZURITE_URL=http://127.0.0.1:10000/devstoreaccount1 \
  uv run pytest tests/contract tests/integration/test_azure_blob.py -q
```

For a **real Azure account**, `configs/storage-backends.example.yaml` shows Shared Key,
SAS and Entra ID setups; this has not been tested yet. **MinIO** goes through the `s3`
adapter with `endpoint_url` (see the same file). CI runs the S3 contract suite against
MinIO.

## 7. Compute lifecycle

**Docker (tested for real).** In a copy of `configs/gpu-infrastructure.example.yaml`:
1. Keep the `docker-gpu` environment.
2. Change the image to something small, e.g. `nginx:1.29.2`, with `container_port: 80`,
   and drop `gpus` if you have no NVIDIA GPU.
3. Start with that profile.

On **Infrastructure**, Provision, Start, Stop and Deprovision appear, and the endpoint
turns *reachable* once it starts. A container you created yourself with the same name is
refused with 409.

**Kubernetes (kind or minikube on the laptop).** This would be the first run against a
real cluster:

```bash
kind create cluster --name omf
kubectl create namespace inference
kubectl -n inference create serviceaccount omf
kubectl -n inference create role omf --verb=get,create,patch,delete \
  --resource=deployments,deployments/scale,services
kubectl -n inference create rolebinding omf --role=omf --serviceaccount=inference:omf
export K8S_TOKEN=$(kubectl -n inference create token omf)
kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.server}'; echo
```

Use the `k8s-gpu` environment from the example with:
* `api_server: <that URL>`, `token_ref: env:K8S_TOKEN`, `verify_tls: false` (kind uses a
  self-signed CA);
* `in_cluster: false`, `gpus: 0`, `probe_endpoint: false`;
* a small image.

For the API to reach the kind API server on loopback, set
`security.egress.allow_loopback: true`.

**AWS / Google Cloud / Azure:** the example shows every field. Start with
`allow_lifecycle: false`: the Infrastructure page then only *describes* the environment
(state, size, GPU), which is a safe first real-account test. Then enable lifecycle in a
sandbox account. Use least-privilege credentials as listed in
`docs/deployment/README.md`.

## Reporting problems

For each issue, note the step, the request id (the `X-Request-ID` response header, or
the code under an error in the UI) and the API log line with that id.
