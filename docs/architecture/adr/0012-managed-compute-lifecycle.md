# ADR-0012: Managed compute lifecycle is opt-in and ownership-bound

**Status:** Accepted

## Context
OMF describes where inference servers run. Operators also want OMF to create, start,
stop and delete that infrastructure: a vLLM container on a Docker host, a Deployment in
Kubernetes, or a GPU VM on AWS, Google Cloud or Azure. These operations cost money and
can destroy work. A misconfigured name must never let OMF stop or delete something
another team owns. An API-level bug or a double click must never launch two GPU VMs.

## Decision
- Five adapters share one base class (`adapters/compute/_managed.py`): `docker`,
  `kubernetes`, `aws_ec2`, `gcp_compute` and `azure_vm`. Each uses the platform's
  official API: the Docker Engine API, the Kubernetes REST API, boto3 for EC2, and the
  Compute Engine and Azure Resource Manager REST APIs. No CLIs are shelled out to.
- **Opt-in.** Lifecycle operations are refused with `lifecycle_disabled` (HTTP 403)
  unless the environment sets `allow_lifecycle: true`. Only admins may call
  `POST /api/v1/compute/{id}/{provision|start|stop|deprovision}`, and every call is
  audited. Read-only description always works.
- **Ownership.** Adapters label or tag what they create (`omf-managed=true`,
  `omf-environment=<id>`). They refuse (`conflict`, 409) to modify or delete a resource
  that has the configured name but lacks the labels. They never adopt it silently.
  - EC2 has one exception: an operator may designate an existing instance
    (`instance_id`). OMF may start and stop it but never terminates it.
  - Azure VMs are created outside OMF (they need a NIC, VNet and disks), so the Azure
    adapter implements only `start` and `stop`. `stop` always **deallocates**, because a
    VM that is only powered off is still billed.
- **One operation at a time** per environment (an asyncio lock in the process).
  Provisioning is idempotent: it looks for the labelled resource first.
- **Bounded waiting.** Operations return when the backend has accepted them. Callers can
  pass `wait=true` with a timeout to poll until the target state is reached.
- **Secrets by reference.** Engine and cloud credentials, and secret environment
  variables for containers, are `env:`/`file:` references (ADR-0010). For Kubernetes,
  secrets are named Kubernetes Secrets, and their values never pass through OMF.
- **Degrade, don't fail.** If an environment's secret is missing, that environment is
  shown as `unknown` with a note. The rest of the platform starts normally.

## Consequences
- The safe default is read-only. Turning on lifecycle is a deliberate configuration
  change, made per environment.
- The lock is per process. With several API replicas, two admins could race. The labels
  still prevent touching foreign resources, and provisioning re-checks before creating,
  but there is a window between check and create. Run one replica for lifecycle
  operations, or put them behind a queue, if this matters.
- Tests:
  - Docker is tested against a real Docker Engine.
  - Kubernetes, Compute Engine and Azure are tested against in-memory fakes of their
    REST endpoints.
  - EC2 is tested against moto.
  - None of them has been run against a real cluster or cloud account. See
    `docs/implementation-status.md`.

## Alternatives considered
- **Terraform/Pulumi from OMF:** these are powerful, but they add state backends and a
  second source of truth. Operators can still create resources that way and point OMF
  at them (Azure, designated EC2 instances).
- **Cloud SDKs everywhere:** boto3 is already an optional dependency. The Google and
  Azure SDKs are large. Their REST APIs are stable and are called with httpx, which OMF
  already uses.
- **Adopting resources by name:** this is convenient, but one typo could stop someone
  else's production workload. It was rejected.
