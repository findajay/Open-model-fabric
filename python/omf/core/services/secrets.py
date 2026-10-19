"""Resolves secret references by dispatching on the reference scheme."""

from __future__ import annotations

from omf.core.domain.errors import ConfigurationError, SecretNotFound
from omf.core.ports.secrets import SecretProvider, SecretRef, SecretReferenceInfo, SecretValue


class SecretResolver:
    def __init__(self, providers: list[SecretProvider]) -> None:
        self._by_scheme: dict[str, SecretProvider] = {}
        for provider in providers:
            for scheme in provider.schemes:
                if scheme in self._by_scheme:
                    raise ConfigurationError(f"secret scheme {scheme!r} registered twice")
                self._by_scheme[scheme] = provider

    @property
    def schemes(self) -> list[str]:
        return sorted(self._by_scheme)

    def resolve(self, reference: str) -> SecretValue:
        ref = SecretRef.parse(reference)
        provider = self._by_scheme.get(ref.scheme)
        if provider is None:
            raise SecretNotFound(
                f"no secret provider for scheme {ref.scheme!r}",
                details={"reference": str(ref), "schemes": self.schemes},
            )
        return provider.resolve_secret(ref)

    def is_available(self, reference: str) -> bool:
        try:
            self.resolve(reference)
        except SecretNotFound:
            return False
        return True

    def list_references(self) -> list[SecretReferenceInfo]:
        out: list[SecretReferenceInfo] = []
        for provider in {id(p): p for p in self._by_scheme.values()}.values():
            out.extend(provider.list_secret_references())
        return sorted(out, key=lambda i: i.reference)
