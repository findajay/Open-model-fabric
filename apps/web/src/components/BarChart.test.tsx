import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { BarChart } from "./BarChart";

describe("BarChart", () => {
  it("renders values at the bar tip and a tooltip on hover", () => {
    render(
      <BarChart
        ariaLabel="latency"
        format={(v) => `${v} ms`}
        data={[
          { label: "mock", value: 100, detail: ["p95 120 ms"] },
          { label: "ollama", value: null },
        ]}
      />,
    );
    expect(screen.getByRole("img", { name: "latency" })).toBeInTheDocument();
    expect(screen.getByText("100 ms")).toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument();
    fireEvent.mouseEnter(screen.getByText("mock").parentElement!);
    expect(screen.getByRole("tooltip")).toHaveTextContent("p95 120 ms");
  });
});
