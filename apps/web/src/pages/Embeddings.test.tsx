import type { ModelDeployment } from "@omf/sdk";
import { describe, expect, it } from "vitest";

import { deploymentValue, deploymentsWith, parseDeploymentValue } from "@/lib/deployments";

import { cosineMatrix } from "./Embeddings";

function deployment(capabilities: string[], state = "available"): ModelDeployment {
  return {
    id: "p/m",
    provider_id: "p",
    provider_model: "org/m",
    state,
    capabilities: {
      claims: capabilities.map((c) => ({
        capability: c as ModelDeployment["capabilities"]["claims"][number]["capability"],
        evidence: "declared" as const,
        source: "test",
        checked_at: null,
      })),
    },
  } as ModelDeployment;
}

describe("embeddings helpers", () => {
  it("computes cosine similarity for unnormalised vectors", () => {
    const m = cosineMatrix([
      [1, 0],
      [2, 0],
      [0, 3],
      [0, 0],
    ]);
    expect(m[0]?.[1]).toBeCloseTo(1);
    expect(m[0]?.[2]).toBeCloseTo(0);
    expect(m[3]?.[3]).toBe(0); // zero vector: no division by zero
  });

  it("filters deployments by capability and availability", () => {
    const embed = deployment(["embeddings"]);
    expect(deploymentsWith([embed, deployment(["chat"])], "embeddings")).toEqual([embed]);
    expect(deploymentsWith([deployment(["embeddings"], "unavailable")], "embeddings")).toEqual([]);
  });

  it("round-trips provider and model names containing slashes", () => {
    expect(parseDeploymentValue(deploymentValue(deployment([])))).toEqual({
      provider_id: "p",
      model: "org/m",
    });
    expect(parseDeploymentValue("")).toEqual({ provider_id: null, model: null });
  });
});
