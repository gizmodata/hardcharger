import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryStatus } from "../../src/components/QueryStatus";
import { saveStatusDisplay, storedStatusDisplay } from "../../src/lib/sqlMonitor";

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.restoreAllMocks();
});
describe("SQL Monitor status presentation", () => {
  it("gives icons explicit accessible names and tooltips, including queue timeouts", () => {
    render(<QueryStatus query={{ status: "timeout", cancel_reason: "queue_timeout" }} display="icons" />);
    expect(screen.getByRole("img", { name: "Queue timed out" }).closest("span")?.title).toBe("Queue timed out");
  });
  it("keeps the executing circle in both presentations", () => {
    const view = render(<QueryStatus query={{ status: "executing", cancel_reason: null }} display="icons" />);
    expect(screen.getByRole("img", { name: "Executing" }).closest("span")?.className).toContain(
      "monitor-status-executing",
    );
    view.rerender(<QueryStatus query={{ status: "executing", cancel_reason: null }} display="descriptions" />);
    expect(screen.getByText("Executing").querySelector("svg")).not.toBeNull();
  });
  it("remembers descriptions and works when browser storage is unavailable", () => {
    expect(storedStatusDisplay()).toBe("icons");
    saveStatusDisplay("descriptions");
    expect(storedStatusDisplay()).toBe("descriptions");
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(storedStatusDisplay()).toBe("icons");
  });
});
