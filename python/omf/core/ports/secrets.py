"""Secret provider port.

Configuration never contains secret values, only references such as
``env:OPENAI_API_KEY`` or ``file:/run/secrets/openai``. A :class:`SecretValue` hides
its content from ``repr``/``str`` so that it cannot leak into logs or API responses by
accident; code that needs the raw value must call :meth:`SecretValue.reveal`.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Annotated

from pydantic import Field

from omf.core.domain.base import DomainModel
from omf.core.domain.errors import ValidationFailed

SECRET_REF_PATTERN = re.compile(r"^(?P<scheme>[a-z]+):(?P<name>[A-Za-z0-9_./-]{1,512})$")

SecretRefStr = Annotated[
    str,
    Field(
        pattern=r"^[a-z]+:[A-Za-z0-9_./-]{1,512}$",
        description="Reference to a secret, e.g. 'env:OPENAI_API_KEY'. Never the secret itself.",
        examples=["env:OPENAI_API_KEY", "file:/run/secrets/openai_api_key"],
    ),
]


class SecretRef(DomainModel):
    scheme: str
    name: str

    @classmethod
    def parse(cls, ref: str) -> SecretRef:
        match = SECRET_REF_PATTERN.match(ref)
        if not match:
            raise ValidationFailed(
                "secret reference must look like 'env:NAME' or 'file:/path'",
                details={"reference_format": "<scheme>:<name>"},
            )
        return cls(scheme=match["scheme"], name=match["name"])

    def __str__(self) -> str:
        return f"{self.scheme}:{self.name}"


class SecretValue:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "SecretValue('**********')"

    __str__ = __repr__


class SecretReferenceInfo(DomainModel):
    reference: str
    available: bool


class SecretProvider(ABC):
    schemes: tuple[str, ...] = ()

    @abstractmethod
    def resolve_secret(self, ref: SecretRef) -> SecretValue:
        """Return the secret or raise ``SecretNotFound``. Never logs the value."""

    @abstractmethod
    def list_secret_references(self) -> list[SecretReferenceInfo]:
        """References this provider knows about, with availability. Never the values."""
