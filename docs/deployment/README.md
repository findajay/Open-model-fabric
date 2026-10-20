# Deployment guide

Open Model Fabric is one process (the FastAPI API, which can also serve the built web UI)
plus whatever inference engines you point it at. Inference engines are deployed
separately and reached over HTTP; moving an engine between a laptop, Docker, Colab or
Kubernetes changes a URL in configuration, not code.

> **Verification status.** The Compose file is validated with `docker compose config`,
> the Kubernetes manifests are parsed and their embedded profile is validated by the test
> suite, and the Dockerfile's dependency and build steps were reproduced outside Docker.
> The container image itself is built by the `docker` CI job; it could not be built in
> the development sandbox (no Docker daemon). Nothing here was tested against a real
> GPU cluster or cloud account.

## Profiles

| Profile | Inference | Compute | Storage | Metadata | Auth |
|---|---|---|---|---|---|
| `development` | mock (+ optional Ollama) | local | local filesystem | SQLite | none (localhost only) |
| `local-ollama` | Ollama (+ mock) | local | local filesystem | SQLite | none (localhost only) |
| `colab-experiment` | mock; optional *authorized* Colab endpoint | local + Colab notebook (manual) | local + Google Drive | SQLite | none (localhost only) |
| `docker` | mock (+ Ollama service) | local | local filesystem (`/data`) | SQLite (`/data`) | API key |
| `production.example` | vLLM via OpenAI-compatible | dedicated GPU pool | S3-compatible | PostgreSQL | API keys |

Select one with `OMF_PROFILE=<name>` (reads `configs/<name>.yaml`) or `OMF_CONFIG=/path`.
Profiles never contain secrets: only references such as `env:VLLM_API_KEY` or
`file:/run/secrets/omf_admin_token`.

## Single container

```bash
docker build -f infrastructure/docker/Dockerfile -t open-model-fabric .
docker run --rm -p 127.0.0.1:8080:8080 -e OMF_ADMIN_TOKEN=$(openssl rand -hex 24) \
  -v omf-data:/data open-model-fabric
```

The image runs as a non-root user, serves the web UI at `/` and the API at `/api/v1`,
uses `configs/docker.yaml` by default, and stores SQLite plus local artifacts in
`/data`.

## Docker Compose

```bash
cp .env.example .env              # set OMF_ADMIN_TOKEN
make compose-up                   # API + UI on http://127.0.0.1:8080 (OMF_PORT to change)
make compose-up PROFILE=ollama    # + an Ollama container
docker compose --env-file .env -f infrastructure/compose/docker-compose.yml exec ollama ollama pull qwen2.5:0.5b
```

Run Compose from the repository root and pass `--env-file .env` when calling it directly
(`make` does this for you): Compose otherwise reads `.env` from the compose file's
directory, `infrastructure/compose/`, and fails with
`required variable OMF_ADMIN_TOKEN is missing a value`. If host port 8080 is taken, set
`OMF_PORT` in `.env`; the container still listens on 8080.

To enable the Ollama provider in that stack, set `OMF_ENABLE_OLLAMA=true` in `.env`.
GPU access for Ollama needs the NVIDIA Container Toolkit; the `deploy.resources` block
in the compose file shows where to enable it.

## Kubernetes

`infrastructure/kubernetes` is a kustomize base:

* `api.yaml` — Deployment and Service for the API. It runs non-root with a read-only
  root filesystem, drops all capabilities and has probes on `/health/live` and
  `/health/ready`.
* `configmap.yaml` — a production profile (PostgreSQL via `OMF_DATABASE_URL`, an
  in-cluster vLLM provider, API keys from a Secret).
* `networkpolicy.yaml` — default-deny egress except DNS, vLLM and PostgreSQL. This is
  defence in depth behind the application's own egress policy.
* `vllm-gpu.yaml` — optional vLLM OpenAI-compatible server on one `nvidia.com/gpu`.

```bash
kubectl create namespace open-model-fabric
kubectl -n open-model-fabric create secret generic omf-secrets \
  --from-literal=omf_admin_token="$(openssl rand -hex 32)" \
  --from-literal=OMF_APP_TOKEN="$(openssl rand -hex 32)" \
  --from-literal=OMF_DATABASE_URL='postgresql+psycopg://omf:...@postgres:5432/omf' \
  --from-literal=VLLM_API_KEY="$(openssl rand -hex 32)"
kubectl apply -k infrastructure/kubernetes
```

Run database migrations as a separate step if you prefer:
`python -m omf.bootstrap.migrate`. The API also migrates on start.

**Scaling.** SQLite is single-writer, so run one replica unless the metadata store is
PostgreSQL. With several replicas, set `server.cancellation: shared`: in-flight requests
are registered in the database, and `POST /chat/requests/{id}/cancel` reaches the replica
serving the stream within about 0.5 s, so sticky sessions are not needed. Closing the
client connection always works. Rate limits are per replica (see the threat model).

## Dedicated GPU inference

Run any OpenAI-compatible engine on GPU hosts or VMs, for example:

```bash
docker run --gpus all -p 8000:8000 vllm/vllm-openai:v0.32.0 \
  --model Qwen/Qwen2.5-7B-Instruct --api-key "$VLLM_API_KEY"
```

Then register it with adapter `openai_compatible`, `base_url: http://<host>:8000/v1`,
`api_key_ref: env:VLLM_API_KEY` and `locality: local`. Declare model capabilities in the
provider's `models:` list (the OpenAI protocol does not report them) and run
*Model Registry → Verify*.

### Letting OMF manage the GPU infrastructure

OMF can also run the inference server for you. These compute adapters create, start,
stop and delete it:

- VMs and containers: `docker`, `kubernetes`, `aws_ec2`, `gcp_compute` and `azure_vm`
  (Azure creates the NIC and VM in an existing subnet);
- container services: `aws_ecs`, `gcp_cloud_run` and `azure_container_apps`.

`configs/gpu-infrastructure.example.yaml` has one environment of each kind. The rules ([ADR-0012](../architecture/adr/0012-managed-compute-lifecycle.md)):

- Nothing changes unless the environment sets `allow_lifecycle: true`. Even then, only
  admins can trigger operations (*Infrastructure* screen, or
  `POST /api/v1/compute/{id}/{provision|start|stop|deprovision}?wait=true`). Every call
  is audited.
- OMF labels what it creates and refuses to touch anything else with the same name.
- Give the credentials the least privilege that works:
  - Docker: access to the socket, which is root-equivalent on that host. Prefer a
    dedicated GPU host.
  - Kubernetes: one namespace, plus optional `list nodes`.
  - AWS: `ec2:Describe*`, `RunInstances`, `CreateTags`, `Start/Stop/TerminateInstances`,
    ideally conditioned on the `omf-managed` tag.
  - Google Cloud: `compute.instances.*` in one project. For Cloud Run, *Cloud Run
    Developer* plus *Service Account User* on the runtime service account.
  - AWS ECS: `ecs:RegisterTaskDefinition`, `DeregisterTaskDefinition`,
    `Create/Update/Delete/DescribeServices`, `DescribeTaskDefinition`, `TagResource`, and
    `iam:PassRole` for the task roles.
  - Azure: *Virtual Machine Contributor* on one resource group, plus *Network
    Contributor* on the subnet when OMF creates VMs. For Container Apps, *Container Apps
    Contributor* on the resource group.
- Compute adapters manage *where* the server runs. Register the server itself as an
  `openai_compatible` or `ollama` provider with `compute_environment_id` set to the
  environment.

Only the Docker adapter has been tested against the real service. The others were tested
against fakes:
- Kubernetes, Compute Engine, Cloud Run, Azure VMs and Container Apps against fakes of
  their REST APIs;
- EC2 and ECS against moto.

Try them in a sandbox account first.

What "stop" means differs by platform:
- **ECS:** desired count 0.
- **Cloud Run:** scale to zero. The service still answers requests with a cold start,
  so deprovision it to make it unreachable.
- **Container Apps:** Azure's stop action; the app runs no replicas.
- **VMs:** stopped. Azure VMs are deallocated.

## Other container platforms (ECS, Cloud Run, Container Apps)

OMF is one stateless container plus a database, so any container platform can host it.
This has **not been tested**. The requirements are the same as for Kubernetes:

- use PostgreSQL and S3 (or another shared store) when you run more than one task;
- set `server.cancellation: shared` (or route a stream's cancel request to the same task
  with sticky sessions);
- raise the load balancer's idle timeout above your longest stream (AWS ALB defaults to
  60 s).

Hosting OMF itself is separate from where models run. To let OMF run *inference
servers* on these platforms, use the `aws_ecs`, `gcp_cloud_run` and
`azure_container_apps` compute adapters. ECS GPUs need EC2 capacity, because Fargate has
no GPUs. Cloud Run offers one NVIDIA L4 per instance in some regions. Container Apps
needs a GPU workload profile on the environment.

## Google Colab

Colab is an interactive notebook product, not a serving platform. Use
`notebooks/colab_model_lab.ipynb` standalone for experiments and benchmarks, and keep its
exports in Google Drive (the `google_drive` storage adapter can read them). Register a
Colab-hosted endpoint in `configs/colab-experiment.yaml` **only** if your Colab plan and
Google's terms permit it, and attest that with `endpoint.authorized: true`. Unauthorized
endpoints are recorded but never contacted. Open Model Fabric never keeps a runtime
alive or reconnects it.

## Production checklist

* `environment: production`. The validator then requires API-key auth and rejects
  wildcard CORS.
* Provide tokens and keys through `/run/secrets` or the environment. Never put them in
  YAML.
* Terminate TLS at a reverse proxy. Disable response buffering for
  `/api/v1/chat/stream`, and add rate limiting there.
* Keep `security.egress.allow_loopback: false`, list the inference hosts in
  `allowed_hosts`, and add a NetworkPolicy or egress firewall.
* PostgreSQL for metadata, with backups and encryption at rest (conversations contain
  prompts).
* Optional OpenTelemetry export: `telemetry.exporters: [logging, otlp]` and
  `otlp_endpoint`.
* Run `make audit` (pip-audit + pnpm audit) in CI. The `security` workflow does this
  nightly.
