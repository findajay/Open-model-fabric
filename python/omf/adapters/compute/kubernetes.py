"""Kubernetes compute adapter: an OMF-managed Deployment + Service running an inference server.

Uses the Kubernetes API server's REST API with httpx (no client library).

* ``provision`` creates the Deployment (0 replicas) and a ClusterIP Service.
* ``start`` and ``stop`` scale the Deployment between ``replicas`` and 0.
* ``deprovision`` deletes both objects.

Everything carries ``omf-managed``/``omf-environment`` labels, and objects without them
are never modified. Resources come from node allocatable capacity, including
``nvidia.com/gpu`` and GPU feature-discovery labels, when the credentials may list nodes.

Authentication: a bearer token (``token_ref``), the pod's service account
(``in_cluster: true``), or a client certificate (``client_cert_ref``/``client_key_ref``,
as in kubeconfig files). Grant the identity only what it needs, in one namespace: get,
create, patch and delete on deployments, deployments/scale and services, and optionally
cluster-wide ``list nodes``.
"""

from __future__ import annotations

import os
import re
import ssl
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import Field, field_validator, model_validator

from omf.adapters.compute._managed import (
    ENVIRONMENT_LABEL,
    OWNER_LABEL,
    Inspection,
    ManagedComputeAdapter,
    ManagedComputeConfig,
    owned,
)
from omf.adapters.compute._rest import RestClient
from omf.core.domain.errors import ComputeError, ConfigurationError, Conflict
from omf.core.domain.models import ComputeResources, ComputeState, GpuInfo
from omf.core.ports.secrets import SecretRefStr, SecretValue
from omf.core.security.egress import EgressPolicy

ADAPTER_TYPE = "kubernetes"
SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


class KubernetesComputeConfig(ManagedComputeConfig):
    api_server: str | None = Field(default=None, examples=["https://k8s.example.com:6443"])
    in_cluster: bool = False
    token_ref: SecretRefStr | None = None
    client_cert_ref: SecretRefStr | None = None
    client_key_ref: SecretRefStr | None = None
    ca_cert_ref: SecretRefStr | None = None
    verify_tls: bool = True
    namespace: str = "default"
    name: str
    image: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    env_from_secrets: list[str] = Field(default_factory=list)
    """Names of Kubernetes Secrets exposed as environment (values never pass through OMF)."""
    container_port: int = Field(default=8000, ge=1, le=65535)
    replicas: int = Field(default=1, ge=1, le=100)
    gpus: int = Field(default=0, ge=0, le=16)
    cpu_request: str | None = "500m"
    memory_request: str | None = "1Gi"
    memory_limit: str | None = None
    node_selector: dict[str, str] = Field(default_factory=dict)
    probe_endpoint: bool = False
    """Service DNS only resolves inside the cluster; enable when OMF runs in-cluster."""

    @field_validator("name", "namespace")
    @classmethod
    def _dns(cls, v: str) -> str:
        if not _DNS_LABEL.match(v):
            raise ValueError("must be a valid DNS-1123 label")
        return v

    @model_validator(mode="after")
    def _auth(self) -> KubernetesComputeConfig:
        if not self.in_cluster and not self.api_server:
            raise ValueError("api_server is required unless in_cluster is true")
        if bool(self.client_cert_ref) != bool(self.client_key_ref):
            raise ValueError("client_cert_ref and client_key_ref must be set together")
        if self.endpoint_port is None:
            self.endpoint_port = self.container_port
        return self


def parse_quantity(value: str | int | None) -> float | None:
    """Parse a Kubernetes resource quantity (``3500m``, ``16Gi``, ``2``) into a number."""
    if value is None:
        return None
    text = str(value).strip()
    suffixes = {
        "Ki": 2**10,
        "Mi": 2**20,
        "Gi": 2**30,
        "Ti": 2**40,
        "Pi": 2**50,
        "k": 10**3,
        "K": 10**3,
        "M": 10**6,
        "G": 10**9,
        "T": 10**12,
        "P": 10**15,
        "m": 1e-3,
    }
    for suffix in sorted(suffixes, key=len, reverse=True):
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) * suffixes[suffix]
    return float(text)


def build_ssl_context(
    ca_pem: str | None, cert_pem: str | None, key_pem: str | None, verify: bool
) -> ssl.SSLContext | bool:
    if ca_pem is None and cert_pem is None:
        return verify
    context = ssl.create_default_context(cadata=ca_pem) if ca_pem else ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    if cert_pem and key_pem:
        # ssl can only load certificate chains from files: write them with 0600 permissions
        # into a private temporary directory and remove them immediately after loading.
        with tempfile.TemporaryDirectory() as tmp:
            cert_file, key_file = Path(tmp) / "client.crt", Path(tmp) / "client.key"
            for path, data in ((cert_file, cert_pem), (key_file, key_pem)):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as fh:
                    fh.write(data)
            context.load_cert_chain(cert_file, key_file)
    return context


class KubernetesComputeAdapter(ManagedComputeAdapter):
    kind = ADAPTER_TYPE

    def __init__(
        self,
        environment_id: str,
        config: KubernetesComputeConfig,
        egress: EgressPolicy,
        *,
        token: SecretValue | None = None,
        client_cert: SecretValue | None = None,
        client_key: SecretValue | None = None,
        ca_cert: SecretValue | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        probe_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(environment_id, config, egress, probe_transport=probe_transport)
        if not environment_id[-1].isalnum():
            # Kubernetes label values must start and end with an alphanumeric character.
            raise ConfigurationError(
                f"environment id {environment_id!r} is not a valid Kubernetes label value"
            )
        self.cfg = config
        base_url = config.api_server or ""
        ca_pem = ca_cert.reveal() if ca_cert else None
        token_value = token.reveal() if token else None
        if config.in_cluster:
            base_url = base_url or "https://kubernetes.default.svc"
            try:
                token_value = token_value or (SA_DIR / "token").read_text().strip()
                ca_pem = ca_pem or (SA_DIR / "ca.crt").read_text()
            except OSError as exc:
                raise ConfigurationError(
                    "in_cluster is set but no service-account token is mounted"
                ) from exc
        verify = build_ssl_context(
            ca_pem,
            client_cert.reveal() if client_cert else None,
            client_key.reveal() if client_key else None,
            config.verify_tls,
        )

        async def bearer() -> str:
            return token_value or ""

        self.api = RestClient(
            name=f"Kubernetes API ({base_url})",
            base_url=base_url,
            token=bearer if token_value else None,
            verify=verify,
            transport=transport,
            timeout_s=30.0,
        )
        ns, name = quote(config.namespace), quote(config.name)
        self._deployment_path = f"/apis/apps/v1/namespaces/{ns}/deployments/{name}"
        self._service_path = f"/api/v1/namespaces/{ns}/services/{name}"

    async def aclose(self) -> None:
        await self.api.aclose()

    # ------------------------------------------------------------------ objects

    def _labels(self) -> dict[str, str]:
        return {
            OWNER_LABEL: "true",
            ENVIRONMENT_LABEL: self.environment_id,
            "app.kubernetes.io/name": self.cfg.name,
            "app.kubernetes.io/managed-by": "open-model-fabric",
        }

    def _deployment(self) -> dict[str, Any]:
        c = self.cfg
        resources: dict[str, dict[str, str]] = {"requests": {}, "limits": {}}
        if c.cpu_request:
            resources["requests"]["cpu"] = c.cpu_request
        if c.memory_request:
            resources["requests"]["memory"] = c.memory_request
        if c.memory_limit:
            resources["limits"]["memory"] = c.memory_limit
        if c.gpus:
            resources["limits"]["nvidia.com/gpu"] = str(c.gpus)
        container: dict[str, Any] = {
            "name": "inference",
            "image": c.image,
            "args": c.args,
            "ports": [{"name": "http", "containerPort": c.container_port}],
            "env": [{"name": k, "value": v} for k, v in c.env.items()],
            "envFrom": [{"secretRef": {"name": s}} for s in c.env_from_secrets],
            "resources": resources,
            "readinessProbe": {
                "httpGet": {"path": c.health_path, "port": "http"},
                "periodSeconds": 10,
            },
            "securityContext": {"allowPrivilegeEscalation": False},
        }
        selector = {ENVIRONMENT_LABEL: self.environment_id}
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": c.name, "namespace": c.namespace, "labels": self._labels()},
            "spec": {
                "replicas": 0,
                "selector": {"matchLabels": selector},
                "template": {
                    "metadata": {"labels": {**self._labels()}},
                    "spec": {
                        "containers": [container],
                        "nodeSelector": c.node_selector,
                        "automountServiceAccountToken": False,
                    },
                },
            },
        }

    def _service(self) -> dict[str, Any]:
        c = self.cfg
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": c.name, "namespace": c.namespace, "labels": self._labels()},
            "spec": {
                "type": "ClusterIP",
                "selector": {ENVIRONMENT_LABEL: self.environment_id},
                "ports": [{"name": "http", "port": c.container_port, "targetPort": "http"}],
            },
        }

    def _check_owned(self, obj: dict[str, Any], what: str) -> None:
        if not owned(obj.get("metadata", {}).get("labels"), self.environment_id):
            raise Conflict(
                f"{what} {self.cfg.namespace}/{self.cfg.name} exists but is not managed "
                f"by Open Model Fabric environment {self.environment_id!r}"
            )

    # ------------------------------------------------------------------ inspection

    async def _node_resources(self) -> tuple[ComputeResources | None, list[str]]:
        try:
            nodes = await self.api.request("GET", "/api/v1/nodes")
        except ComputeError as exc:
            if exc.details.get("status") == 403:
                return None, ["Not permitted to list nodes; cluster capacity unknown."]
            raise
        cpu = memory = 0.0
        gpus: list[GpuInfo] = []
        for node in nodes.get("items", []):
            alloc = node.get("status", {}).get("allocatable", {})
            labels = node.get("metadata", {}).get("labels", {})
            cpu += parse_quantity(alloc.get("cpu")) or 0
            memory += parse_quantity(alloc.get("memory")) or 0
            count = int(parse_quantity(alloc.get("nvidia.com/gpu")) or 0)
            product = labels.get("nvidia.com/gpu.product", "nvidia.com/gpu")
            mem_mib = labels.get("nvidia.com/gpu.memory")
            gpus += [
                GpuInfo(name=product, memory_total_bytes=int(mem_mib) * 2**20 if mem_mib else None)
                for _ in range(count)
            ]
        return ComputeResources(
            cpu_count=int(cpu) or None,
            memory_total_bytes=int(memory) or None,
            gpus=gpus,
            accelerator="cuda" if gpus else "none",
            source="detected",
        ), []

    async def _inspect(self) -> Inspection:
        resources, notes = await self._node_resources()
        deployment = await self.api.request("GET", self._deployment_path, allow_404=True)
        details: dict[str, Any] = {
            "namespace": self.cfg.namespace,
            "deployment": self.cfg.name,
            "image": self.cfg.image,
            "gpus_requested": self.cfg.gpus,
        }
        if deployment is None:
            return Inspection(
                state=ComputeState.OFFLINE,
                exists=False,
                resources=resources,
                notes=notes,
                details=details,
            )
        if not owned(deployment.get("metadata", {}).get("labels"), self.environment_id):
            notes.append(
                "A deployment with this name exists but is not managed by Open Model "
                "Fabric; lifecycle operations will refuse to touch it."
            )
        desired = int(deployment.get("spec", {}).get("replicas") or 0)
        ready = int(deployment.get("status", {}).get("readyReplicas") or 0)
        if desired == 0:
            state = ComputeState.OFFLINE
        elif ready >= desired:
            state = ComputeState.ONLINE
        else:
            state = ComputeState.DEGRADED
        details.update(replicas_desired=desired, replicas_ready=ready)
        host = f"{self.cfg.name}.{self.cfg.namespace}.svc.cluster.local"
        return Inspection(
            state=state,
            exists=True,
            resources=resources,
            endpoint_host=host,
            notes=notes,
            details=details,
        )

    # ------------------------------------------------------------------ lifecycle

    async def _do_provision(self) -> None:
        deployments = f"/apis/apps/v1/namespaces/{quote(self.cfg.namespace)}/deployments"
        services = f"/api/v1/namespaces/{quote(self.cfg.namespace)}/services"
        existing = await self.api.request("GET", self._deployment_path, allow_404=True)
        if existing is None:
            await self.api.request("POST", deployments, json=self._deployment())
        else:
            self._check_owned(existing, "deployment")
        service = await self.api.request("GET", self._service_path, allow_404=True)
        if service is None:
            await self.api.request("POST", services, json=self._service())
        else:
            self._check_owned(service, "service")

    async def _scale(self, replicas: int) -> None:
        deployment = await self.api.request("GET", self._deployment_path, allow_404=True)
        if deployment is None:
            raise ComputeError(
                f"deployment {self.cfg.namespace}/{self.cfg.name} is not provisioned",
                details={"hint": "run provision first"},
            )
        self._check_owned(deployment, "deployment")
        await self.api.request(
            "PATCH",
            f"{self._deployment_path}/scale",
            json={"spec": {"replicas": replicas}},
            headers={"Content-Type": "application/merge-patch+json"},
        )

    async def _do_start(self) -> None:
        await self._scale(self.cfg.replicas)

    async def _do_stop(self) -> None:
        await self._scale(0)

    async def _do_deprovision(self) -> None:
        for path, what in ((self._service_path, "service"), (self._deployment_path, "deployment")):
            obj = await self.api.request("GET", path, allow_404=True)
            if obj is None:
                continue
            self._check_owned(obj, what)
            await self.api.request("DELETE", path, allow_404=True)
