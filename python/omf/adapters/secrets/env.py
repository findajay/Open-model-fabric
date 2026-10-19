"""Secret providers backed by environment variables (``env:``) and files (``file:``).

``file:`` references are restricted to configured directories (default
``/run/secrets``, the Docker/Kubernetes convention) so a configuration cannot be used to
read arbitrary files from the server.
"""

from __future__ import annotations

import os
from pathlib import Path

from omf.core.domain.errors import SecretNotFound
from omf.core.ports.secrets import SecretProvider, SecretRef, SecretReferenceInfo, SecretValue

MAX_SECRET_BYTES = 64 * 1024


class EnvSecretProvider(SecretProvider):
    schemes = ("env",)

    def __init__(
        self, known_references: list[str] | None = None, environ: dict[str, str] | None = None
    ) -> None:
        self._known = sorted({r for r in (known_references or []) if r.startswith("env:")})
        self._environ = environ if environ is not None else os.environ

    def resolve_secret(self, ref: SecretRef) -> SecretValue:
        value = self._environ.get(ref.name)
        if not value:
            raise SecretNotFound(
                f"environment variable {ref.name} is not set", details={"reference": str(ref)}
            )
        return SecretValue(value)

    def list_secret_references(self) -> list[SecretReferenceInfo]:
        return [
            SecretReferenceInfo(reference=r, available=bool(self._environ.get(r[4:])))
            for r in self._known
        ]


class FileSecretProvider(SecretProvider):
    schemes = ("file",)

    def __init__(self, allowed_dirs: list[str], known_references: list[str] | None = None) -> None:
        self._allowed = [Path(d).resolve() for d in allowed_dirs]
        self._known = sorted({r for r in (known_references or []) if r.startswith("file:")})

    def _path(self, ref: SecretRef) -> Path:
        path = Path(ref.name).resolve()
        if not any(path.is_relative_to(d) for d in self._allowed):
            raise SecretNotFound(
                "file secret is outside the allowed secret directories",
                details={"reference": str(ref), "allowed_dirs": [str(d) for d in self._allowed]},
            )
        return path

    def resolve_secret(self, ref: SecretRef) -> SecretValue:
        path = self._path(ref)
        try:
            if path.stat().st_size > MAX_SECRET_BYTES:
                raise SecretNotFound("secret file is too large", details={"reference": str(ref)})
            return SecretValue(path.read_text(encoding="utf-8").strip())
        except OSError as exc:
            raise SecretNotFound(
                f"secret file not readable ({type(exc).__name__})", details={"reference": str(ref)}
            ) from exc

    def list_secret_references(self) -> list[SecretReferenceInfo]:
        out = []
        for r in self._known:
            try:
                available = self._path(SecretRef.parse(r)).is_file()
            except SecretNotFound:
                available = False
            out.append(SecretReferenceInfo(reference=r, available=available))
        return out
