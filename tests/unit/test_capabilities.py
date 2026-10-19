from __future__ import annotations

from omf.core.domain.capabilities import Capability, CapabilityClaim, CapabilitySet, Evidence


def test_strongest_evidence_wins() -> None:
    declared = CapabilitySet.declared([Capability.CHAT, Capability.VISION_INPUT], "config")
    verified = CapabilitySet(
        claims=(
            CapabilityClaim(capability=Capability.CHAT, evidence=Evidence.VERIFIED, source="probe"),
        )
    )
    merged = declared.merge(verified)
    assert merged.get(Capability.CHAT).evidence == Evidence.VERIFIED  # type: ignore[union-attr]
    assert merged.get(Capability.VISION_INPUT).evidence == Evidence.DECLARED  # type: ignore[union-attr]
    # merging in the other order gives the same answer
    assert verified.merge(declared) == merged


def test_undeclared_capabilities_are_unsupported() -> None:
    caps = CapabilitySet.declared([Capability.CHAT], "config")
    assert caps.supports(Capability.CHAT)
    assert not caps.supports(Capability.VISION_INPUT)
    assert caps.missing([Capability.CHAT, Capability.TOOL_CALLING]) == [Capability.TOOL_CALLING]


def test_empty_set_supports_nothing() -> None:
    assert CapabilitySet().missing(list(Capability)) == list(Capability)
