import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ProfilePlan } from "../../src/components/ProfilePlan";
import { estimatedRowsText, operatorInsights, parseProfile } from "../../src/lib/queryProfile";

const profile = parseProfile(
  JSON.stringify({
    operator: [
      {
        type: "ROOT",
        timing: 0,
        children: [
          { type: "SMALL", timing: 0.00001, children: [{ type: "TINY", timing: 0.00001 }] },
          { type: "SCAN", timing: 1.2, intermediate_rows: 1000000, extra_info: { "Estimated Cardinality": "1000" } },
          { type: "MISSING" },
        ],
      },
    ],
  }),
  new Map(),
);
afterEach(cleanup);

describe("operator comparisons", () => {
  it("formats string cardinalities without rounding large integers or inventing missing values", () => {
    expect(estimatedRowsText("55227")).toBe((55227).toLocaleString());
    expect(estimatedRowsText("9007199254740993")).toBe(9007199254740993n.toLocaleString());
    expect(estimatedRowsText("0")).toBe("0");
    expect(estimatedRowsText(undefined)).toBe("—");
    expect(estimatedRowsText("unknown")).toBe("unknown");
  });
  it("finds slow operators, entire small subtrees and uncertain measurements", () => {
    const insights = operatorInsights(profile.operators);
    expect(insights[0]).toMatchObject({ end: 5, small: false });
    expect(insights[1]).toMatchObject({ end: 3, small: true });
    expect(insights[3]).toMatchObject({
      fraction: 1,
      hot: true,
      durationBand: "long",
      estimateError: "1,000× underestimate",
    });
    expect(insights[4]).toMatchObject({ small: false, hot: false });
    expect(insights[1]?.durationBand).toBe("short");
    const bands = operatorInsights(
      [1, 0.5, 0.2, 0.1, 0.01].map((timing) => ({ name: "OP", depth: 0, timing, extra: {}, metrics: [] })),
    );
    expect(bands.map((op) => op.durationBand)).toEqual(["long", "long", "medium", "medium", "short"]);
  });
  it("sorts independently of tree collapse and restores the original plan order", () => {
    render(<ProfilePlan profile={profile} />);
    const table = screen.getByTestId("query-profile-plan");
    expect(within(table).getAllByRole("row")).toHaveLength(6);
    expect(within(table).getByText("1,000")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Collapse small subtrees" }));
    expect(within(table).queryByText("TINY")).toBeNull();
    expect(within(table).getByText("SMALL")).toBeTruthy();
    expect(within(table).getByText("MISSING")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Operator order"), { target: { value: "slow" } });
    expect(within(table).getAllByRole("row")[1]?.textContent).toContain("SCAN");
    expect(within(table).getAllByRole("row")).toHaveLength(6);
    fireEvent.change(screen.getByLabelText("Operator order"), { target: { value: "plan" } });
    fireEvent.click(screen.getByRole("button", { name: "Expand all" }));
    expect(within(table).getAllByRole("row")[1]?.textContent).toContain("ROOT");
    expect(within(table).getAllByRole("row")).toHaveLength(6);
  });
  it("does not collapse a truncated subtree or draw nonzero bars for zero durations", () => {
    const zero = parseProfile('{"operator":[{"type":"ZERO","timing":0}]}', new Map());
    expect(operatorInsights(zero.operators)[0]).toMatchObject({ fraction: 0, small: false, hot: false });
    render(<ProfilePlan profile={{ ...profile, operatorsTruncated: true }} />);
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Collapse small subtrees" }).disabled).toBe(true);
  });
});
