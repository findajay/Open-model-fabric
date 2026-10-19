import type { Capability, Evidence, ModelDeployment } from "@omf/sdk";

/** Capabilities a deployment claims (any evidence). Undeclared = unsupported. */
export function claims(deployment: ModelDeployment | undefined): Map<Capability, Evidence> {
  const out = new Map<Capability, Evidence>();
  for (const claim of deployment?.capabilities?.claims ?? [])
    out.set(claim.capability, claim.evidence);
  return out;
}

export function supports(deployment: ModelDeployment | undefined, capability: Capability): boolean {
  return claims(deployment).has(capability);
}

export const CAPABILITY_LABELS: Record<Capability, string> = {
  text_generation: "Text",
  chat: "Chat",
  vision_input: "Vision",
  embeddings: "Embeddings",
  tool_calling: "Tools",
  structured_output: "JSON",
  streaming: "Streaming",
  audio_input: "Audio in",
  audio_output: "Audio out",
};

export const EVIDENCE_LABELS: Record<Evidence, string> = {
  verified: "verified by OMF probe",
  provider_reported: "reported by the inference engine",
  declared: "declared, not verified",
};
