import type { ModelDeployment } from "@omf/sdk";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { capabilitiesFor } from "@/pages/Playground";

import { Composer } from "./Composer";

function deployment(capabilities: string[], provider = "p", model = "m"): ModelDeployment {
  return {
    id: `${provider}/${model}`,
    provider_id: provider,
    provider_model: model,
    model_id: null,
    compute_environment_id: null,
    state: "available",
    capabilities: {
      claims: capabilities.map((c) => ({
        capability: c as ModelDeployment["capabilities"]["claims"][number]["capability"],
        evidence: "declared" as const,
        source: "test",
        checked_at: null,
      })),
    },
    context_window: null,
    quantization: null,
    family: null,
    details: {},
    discovered_at: null,
  };
}

describe("capability-driven composer (acceptance criterion 9)", () => {
  it("hides image upload and JSON mode for a text-only model", () => {
    const caps = capabilitiesFor([deployment(["chat", "streaming"])]);
    render(
      <Composer capabilities={caps} streaming={false} onSend={() => {}} onCancel={() => {}} />,
    );
    expect(screen.queryByRole("button", { name: /attach image/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: /json mode/i })).not.toBeInTheDocument();
  });

  it("shows image upload only when the model declares vision_input", () => {
    const caps = capabilitiesFor([deployment(["chat", "vision_input"])]);
    render(
      <Composer capabilities={caps} streaming={false} onSend={() => {}} onCancel={() => {}} />,
    );
    expect(screen.getByRole("button", { name: /attach image/i })).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: /json mode/i })).not.toBeInTheDocument();
  });

  it("shows JSON mode only when the model declares structured_output", () => {
    const caps = capabilitiesFor([deployment(["chat", "structured_output"])]);
    render(
      <Composer capabilities={caps} streaming={false} onSend={() => {}} onCancel={() => {}} />,
    );
    expect(screen.getByRole("switch", { name: /json mode/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /attach image/i })).not.toBeInTheDocument();
  });

  it("offers a capability under routing only if every candidate deployment declares it", () => {
    expect(
      capabilitiesFor([deployment(["chat", "vision_input"]), deployment(["chat"], "q")]),
    ).toEqual({
      vision: false,
      json: false,
    });
    expect(capabilitiesFor([])).toEqual({ vision: false, json: false });
  });

  it("sends text and attached images, and validates image type", async () => {
    const onSend = vi.fn();
    const { container } = render(
      <Composer
        capabilities={{ vision: true, json: true }}
        streaming={false}
        onSend={onSend}
        onCancel={() => {}}
      />,
    );
    const input = container.querySelector('[data-testid="image-input"]') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(["x"], "doc.pdf", { type: "application/pdf" })] },
    });
    expect(await screen.findByText(/only PNG, JPEG, WebP or GIF/)).toBeInTheDocument();
    fireEvent.change(input, {
      target: { files: [new File(["png"], "cat.png", { type: "image/png" })] },
    });
    expect(await screen.findByText("cat.png")).toBeInTheDocument();
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "what is this?");
    await userEvent.click(screen.getByRole("switch", { name: /json mode/i }));
    await userEvent.click(screen.getByRole("button", { name: /send/i }));
    expect(onSend).toHaveBeenCalledWith({
      text: "what is this?",
      json: true,
      images: [{ name: "cat.png", mediaType: "image/png", dataBase64: "cG5n" }],
    });
  });

  it("shows a stop button while streaming", async () => {
    const onCancel = vi.fn();
    render(
      <Composer
        capabilities={{ vision: false, json: false }}
        streaming
        onSend={() => {}}
        onCancel={onCancel}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /stop/i }));
    expect(onCancel).toHaveBeenCalled();
  });
});
