"""Configuration profiles.

A profile is a YAML file (``configs/*.yaml``) validated against :class:`AppConfig`.

* ``${VAR}`` and ``${VAR:-default}`` are substituted from the environment in string
  values, so the same profile is portable between machines (e.g. ``OLLAMA_BASE_URL``).
* Secrets never appear in profiles. Fields that hold credentials are ``*_ref`` secret
  references (``env:NAME`` / ``file:/run/secrets/name``). The loader rejects any
  credential-looking key with a literal value.
* ``environment: production`` enables stricter validation (authentication required,
  no wildcard CORS, no loopback/private egress unless explicitly allowed).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from omf.core.domain.capabilities import Capability, CapabilitySet, Modality
from omf.core.domain.errors import ConfigurationError
from omf.core.domain.models import (
    SLUG_PATTERN,
    ArtifactRef,
    ModelDefinition,
    ModelLicense,
    ProviderDefinition,
    RoutingPolicy,
)
from omf.core.ports.secrets import SecretRefStr
from omf.core.security.egress import EgressPolicy

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_SECRET_KEY_NAMES = {
    "api_key",
    "apikey",
    "token",
    "access_token",
    "refresh_token",
    "password",
    "passwd",
    "secret",
    "secret_key",
    "client_secret",
    "credentials",
    "private_key",
    "authorization",
}
_SECRET_SUFFIXES = ("_api_key", "_password", "_secret", "_token", "_private_key", "_credentials")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerConfig(_Strict):
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    cors_allow_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    max_request_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=512 * 1024 * 1024)
    max_upload_bytes: int = Field(default=512 * 1024 * 1024, ge=1024)
    serve_web_dir: str | None = None
    """If set, serve the built React app (apps/web/dist) from this directory."""


class ApiToken(_Strict):
    name: str = Field(pattern=SLUG_PATTERN)
    role: Literal["admin", "user"]
    token_ref: SecretRefStr


class AuthConfig(_Strict):
    mode: Literal["none", "api_key"] = "api_key"
    tokens: list[ApiToken] = Field(default_factory=list)


class SecurityConfig(_Strict):
    egress: EgressPolicy = Field(default_factory=EgressPolicy)
    log_prompt_content: bool = False
    secret_dirs: list[str] = Field(default_factory=lambda: ["/run/secrets"])
    allowed_upload_content_types: list[str] = Field(
        default_factory=lambda: [
            "application/octet-stream",
            "application/json",
            "text/plain",
            "text/csv",
            "application/x-yaml",
            "application/gzip",
            "application/zip",
        ]
    )


class MetadataConfig(_Strict):
    url: str | None = "sqlite:///./var/omf.db"
    url_ref: SecretRefStr | None = None
    """Use for database URLs that embed credentials (e.g. PostgreSQL)."""

    @model_validator(mode="after")
    def _one(self) -> MetadataConfig:
        if self.url_ref:
            self.url = None
        if self.url and re.search(r"://[^/@]+:[^/@]+@", self.url):
            raise ValueError("metadata.url must not embed credentials; use metadata.url_ref")
        if not self.url and not self.url_ref:
            raise ValueError("metadata.url or metadata.url_ref is required")
        return self


class ComponentConfig(_Strict):
    id: str = Field(pattern=SLUG_PATTERN)
    adapter: str
    config: dict[str, Any] = Field(default_factory=dict)


class StorageEntry(ComponentConfig):
    default: bool = False


class HealthConfig(_Strict):
    interval_s: float = Field(default=30.0, ge=0, le=3600)
    failure_threshold: int = Field(default=3, ge=1, le=20)


class TelemetryConfig(_Strict):
    exporters: list[Literal["logging", "otlp"]] = Field(default_factory=lambda: ["logging"])  # type: ignore[arg-type]
    otlp_endpoint: str | None = None
    service_name: str = "open-model-fabric"


class InferenceConfig(_Strict):
    default_provider: str | None = None
    default_model: str | None = None
    request_timeout_s: float = Field(default=300.0, gt=0, le=3600)
    routing: RoutingPolicy = Field(default_factory=RoutingPolicy)
    discovery_ttl_s: float = Field(default=30.0, ge=0, le=3600)


class CatalogModel(_Strict):
    """YAML shape of a catalog entry; converted to :class:`ModelDefinition`."""

    id: str
    display_name: str
    family: str | None = None
    revision: str | None = None
    parameters: str | None = None
    description: str | None = None
    input_modalities: list[Modality] = Field(default_factory=lambda: [Modality.TEXT])
    output_modalities: list[Modality] = Field(default_factory=lambda: [Modality.TEXT])
    context_window: int | None = None
    max_output_tokens: int | None = None
    quantizations: list[str] = Field(default_factory=list)
    runtime_compatibility: list[str] = Field(default_factory=list)
    license: ModelLicense | None = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    capabilities: list[Capability] = Field(default_factory=list)
    provider_aliases: list[str] = Field(default_factory=list)

    def to_domain(self) -> ModelDefinition:
        data = self.model_dump(exclude={"capabilities"})
        return ModelDefinition(
            **data, capabilities=CapabilitySet.declared(self.capabilities, "catalog")
        )


class AppConfig(_Strict):
    profile: str
    environment: Literal["development", "production"] = "development"
    server: ServerConfig = Field(default_factory=ServerConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    metadata: MetadataConfig = Field(default_factory=MetadataConfig)
    providers: list[ProviderDefinition] = Field(default_factory=list)
    models: list[CatalogModel] = Field(default_factory=list)
    compute: list[ComponentConfig] = Field(default_factory=list)
    storage: list[StorageEntry] = Field(default_factory=list)
    inference: InferenceConfig = Field(default_factory=InferenceConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)

    @field_validator("providers")
    @classmethod
    def _unique_providers(cls, v: list[ProviderDefinition]) -> list[ProviderDefinition]:
        ids = [p.id for p in v]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate provider ids: {dupes}")
        return v

    @model_validator(mode="after")
    def _cross_checks(self) -> AppConfig:
        provider_ids = {p.id for p in self.providers}
        compute_ids = {c.id for c in self.compute}
        storage_ids = [s.id for s in self.storage]
        if len(set(storage_ids)) != len(storage_ids):
            raise ValueError("duplicate storage ids")
        if sum(1 for s in self.storage if s.default) > 1:
            raise ValueError("at most one storage provider may be the default")
        for p in self.providers:
            if p.compute_environment_id and p.compute_environment_id not in compute_ids:
                raise ValueError(
                    f"provider {p.id!r} references unknown compute environment "
                    f"{p.compute_environment_id!r}"
                )
        inf = self.inference
        if inf.default_provider and inf.default_provider not in provider_ids:
            raise ValueError(
                f"inference.default_provider {inf.default_provider!r} is not a configured provider"
            )
        r = inf.routing
        for pid in [*r.preferred_provider_ids, *r.fallback_provider_ids]:
            if pid not in provider_ids:
                raise ValueError(f"routing references unknown provider {pid!r}")
        if r.mode.value == "explicit":
            raise ValueError(
                "inference.routing.mode 'explicit' is per request (provider_id), not a policy"
            )
        if r.fallback_provider_ids and not r.allow_cross_provider_fallback:
            raise ValueError(
                "routing.fallback_provider_ids requires allow_cross_provider_fallback: true"
            )
        if self.environment == "production":
            if self.auth.mode != "api_key" or not self.auth.tokens:
                raise ValueError(
                    "production profiles must use auth.mode=api_key with at least one token"
                )
            if "*" in self.server.cors_allow_origins:
                raise ValueError("production profiles must not allow CORS from '*'")
        elif self.auth.mode == "none" and self.server.host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(
                "auth.mode=none is only allowed when binding to localhost; set server.host to "
                "127.0.0.1 or enable api_key authentication"
            )
        return self

    def secret_references(self) -> list[str]:
        refs: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    if isinstance(k, str) and k.endswith("_ref") and isinstance(v, str):
                        refs.append(v)
                    walk(v)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(self.model_dump(mode="json"))
        return sorted(set(refs))


# ---------------------------------------------------------------------- loading


def substitute_env(node: Any, environ: Mapping[str, str] | None = None) -> Any:
    env = os.environ if environ is None else environ
    if isinstance(node, str):

        def repl(m: re.Match[str]) -> str:
            value = env.get(m.group(1))
            if value is None:
                if m.group(2) is None:
                    raise ConfigurationError(
                        f"environment variable {m.group(1)} is not set and has no default"
                    )
                return m.group(2)
            return value

        return _ENV_PATTERN.sub(repl, node)
    if isinstance(node, dict):
        return {k: substitute_env(v, env) for k, v in node.items()}
    if isinstance(node, list):
        return [substitute_env(v, env) for v in node]
    return node


def find_inline_secrets(node: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            key = str(k).lower()
            here = f"{path}.{k}" if path else str(k)
            looks_secret = key in _SECRET_KEY_NAMES or key.endswith(_SECRET_SUFFIXES)
            if looks_secret and not key.endswith("_ref") and isinstance(v, str) and v:
                found.append(here)
            found.extend(find_inline_secrets(v, here))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            found.extend(find_inline_secrets(item, f"{path}[{i}]"))
    return found


def parse_config(raw: dict[str, Any], environ: dict[str, str] | None = None) -> AppConfig:
    inline = find_inline_secrets(raw)
    if inline:
        raise ConfigurationError(
            "secrets must not be stored in configuration files; use *_ref secret references",
            details={"fields": inline},
        )
    data = substitute_env(raw, environ)
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigurationError(
            "invalid configuration",
            details={
                "errors": [
                    {"loc": ".".join(str(p) for p in e["loc"]), "msg": e["msg"]}
                    for e in exc.errors(include_url=False, include_input=False)
                ]
            },
        ) from exc


def load_config(path: str | Path, environ: dict[str, str] | None = None) -> AppConfig:
    p = Path(path)
    if not p.is_file():
        raise ConfigurationError(f"configuration file {p} not found")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{p} must contain a YAML mapping")
    catalogs = raw.pop("model_catalogs", []) or []
    base = p.parent.resolve()
    for name in catalogs:
        catalog_path = (base / str(name)).resolve()
        if not catalog_path.is_relative_to(base) or not catalog_path.is_file():
            raise ConfigurationError(f"model catalog {name!r} must be a file next to {p.name}")
        entries = yaml.safe_load(catalog_path.read_text(encoding="utf-8")) or {}
        raw.setdefault("models", []).extend(entries.get("models", []))
    return parse_config(raw, environ)


def resolve_config_path(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    if env.get("OMF_CONFIG"):
        return Path(env["OMF_CONFIG"])
    profile = env.get("OMF_PROFILE", "development")
    if not re.fullmatch(r"[a-z0-9_.-]+", profile):
        raise ConfigurationError(f"invalid profile name {profile!r}")
    root = Path(env.get("OMF_CONFIG_DIR", "configs"))
    return root / f"{profile}.yaml"
