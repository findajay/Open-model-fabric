import type { ComputeEnvironment } from "@omf/sdk";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";

import { ComputeActions } from "./Infrastructure";

function env(operations: string[]): ComputeEnvironment {
  return {
    id: "gpu",
    kind: "kubernetes",
    display_name: "GPU",
    state: "offline",
    operations: operations as ComputeEnvironment["operations"],
  } as ComputeEnvironment;
}

function renderActions(e: ComputeEnvironment) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ComputeActions env={e} />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.restoreAllMocks());

describe("compute lifecycle actions", () => {
  it("renders nothing when lifecycle operations are not allowed", () => {
    const { container } = renderActions(env(["describe", "get_status"]));
    expect(container).toBeEmptyDOMElement();
  });

  it("shows only allowed operations and calls the API", async () => {
    const op = vi.spyOn(api, "computeOperation").mockResolvedValue(env(["start"]));
    renderActions(env(["describe", "start", "stop"]));
    expect(screen.queryByRole("button", { name: /provision/i })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /start/i }));
    expect(op).toHaveBeenCalledWith("gpu", "start");
  });

  it("asks for confirmation before deprovisioning", async () => {
    const op = vi.spyOn(api, "computeOperation").mockResolvedValue(env([]));
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderActions(env(["deprovision"]));
    await userEvent.click(screen.getByRole("button", { name: /deprovision/i }));
    expect(confirm).toHaveBeenCalledOnce();
    expect(op).not.toHaveBeenCalled();
    confirm.mockReturnValue(true);
    await userEvent.click(screen.getByRole("button", { name: /deprovision/i }));
    expect(op).toHaveBeenCalledWith("gpu", "deprovision");
  });
});
