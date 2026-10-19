"""Capability declarations.

The capability registry is authoritative: a model is treated as supporting a capability
only if a claim exists for it. Every claim carries *evidence* so that users can tell the
difference between "the operator wrote this in a config file" and "Open Model Fabric
actually exercised it".
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum

from pydantic import ConfigDict

from omf.core.domain.base import DomainModel


class Capability(StrEnum):
    TEXT_GENERATION = "text_generation"
    CHAT = "chat"
    VISION_INPUT = "vision_input"
    EMBEDDINGS = "embeddings"
    TOOL_CALLING = "tool_calling"
    STRUCTURED_OUTPUT = "structured_output"
    STREAMING = "streaming"
    # Future extension: declared in the vocabulary so catalogs can describe models,
    # but no adapter implements audio request/response shapes yet.
    AUDIO_INPUT = "audio_input"
    AUDIO_OUTPUT = "audio_output"


class Evidence(StrEnum):
    """How much we trust a capability claim, strongest first."""

    VERIFIED = "verified"
    """Open Model Fabric exercised the capability against the deployment and it worked."""

    PROVIDER_REPORTED = "provider_reported"
    """The inference engine reported it in its own metadata (e.g. Ollama ``/api/show``)."""

    DECLARED = "declared"
    """An operator or catalog declared it; it has not been verified."""


_EVIDENCE_RANK = {Evidence.VERIFIED: 3, Evidence.PROVIDER_REPORTED: 2, Evidence.DECLARED: 1}


class Modality(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    EMBEDDING = "embedding"


class CapabilityClaim(DomainModel):
    model_config = ConfigDict(frozen=True)

    capability: Capability
    evidence: Evidence
    source: str
    """Where the claim came from, e.g. ``catalog``, ``config``, ``ollama:/api/show``."""
    checked_at: datetime | None = None


class CapabilitySet(DomainModel):
    """An immutable set of capability claims, at most one (the strongest) per capability."""

    model_config = ConfigDict(frozen=True)

    claims: tuple[CapabilityClaim, ...] = ()

    @classmethod
    def declared(cls, caps: Iterable[Capability | str], source: str) -> CapabilitySet:
        return cls(
            claims=tuple(
                CapabilityClaim(capability=Capability(c), evidence=Evidence.DECLARED, source=source)
                for c in caps
            )
        ).normalized()

    def normalized(self) -> CapabilitySet:
        best: dict[Capability, CapabilityClaim] = {}
        for claim in self.claims:
            current = best.get(claim.capability)
            if current is None or _EVIDENCE_RANK[claim.evidence] > _EVIDENCE_RANK[current.evidence]:
                best[claim.capability] = claim
        return CapabilitySet(claims=tuple(sorted(best.values(), key=lambda c: c.capability.value)))

    def merge(self, other: CapabilitySet) -> CapabilitySet:
        return CapabilitySet(claims=self.claims + other.claims).normalized()

    def supports(self, capability: Capability) -> bool:
        return any(c.capability == capability for c in self.claims)

    def get(self, capability: Capability) -> CapabilityClaim | None:
        for claim in self.claims:
            if claim.capability == capability:
                return claim
        return None

    def missing(self, required: Iterable[Capability]) -> list[Capability]:
        return [c for c in required if not self.supports(c)]

    def names(self) -> list[str]:
        return [c.capability.value for c in self.claims]
