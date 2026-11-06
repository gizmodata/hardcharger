import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { SqlText } from "../../src/components/SqlText";

afterEach(cleanup);
it("toggles a read-only SQL view without changing the original and keeps it across refresh", () => {
  const sql = "select Foo, BAR from T";
  const view = render(<SqlText sql={sql} />);
  expect(screen.getByLabelText("SQL text").textContent).toBe(sql);
  fireEvent.click(screen.getByRole("button", { name: "Formatted" }));
  expect(screen.getByLabelText("SQL text").textContent).toContain("\n  , bar");
  view.rerender(<SqlText sql={sql} />);
  expect(screen.getByLabelText("SQL text").textContent).toContain("\n  , bar");
  fireEvent.click(screen.getByRole("button", { name: "Original" }));
  expect(screen.getByLabelText("SQL text").textContent).toBe(sql);
});
it("keeps truncated internal SQL readable with an explanation", () => {
  render(<SqlText sql="SELECT 'truncated" internal />);
  fireEvent.click(screen.getByRole("button", { name: "Formatted" }));
  expect(screen.getByLabelText("Internal SQL text").textContent).toBe("SELECT 'truncated");
  expect(screen.getByText(/could not be formatted safely/)).toBeTruthy();
});
