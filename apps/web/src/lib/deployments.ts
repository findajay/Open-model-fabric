import type { Capability, ModelDeployment } from "@omf/sdk";

import { supports } from "@/lib/capabilities";

/** Value of a deployment option: "provider::model"; "" lets the router choose. */
export function deploymentValue(d: ModelDeployment): string {
  return `${d.provider_id}::${d.provider_model}`;
}

export function parseDeploymentValue(value: string): {
  provider_id: string | null;
  model: string | null;
} {
  if (!value) return { provider_id: null, model: null };
  const [provider, ...rest] = value.split("::");
  return { provider_id: provider ?? null, model: rest.join("::") || null };
}

/** Available deployments that declare a capability (undeclared = unsupported). */
export function deploymentsWith(
  deployments: ModelDeployment[] | undefined,
  capability: Capability,
): ModelDeployment[] {
  return (deployments ?? []).filter((d) => d.state === "available" && supports(d, capability));
}
