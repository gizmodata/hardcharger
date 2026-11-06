import { render, screen, cleanup } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import type { StatementDocument } from "@gizmodata/chargewire";
import { QueryProgress } from "../../src/components/QueryProgress";

afterEach(cleanup);
const doc = (fields: Partial<StatementDocument>) =>
  ({ statement_id: "q1", state: "running", result: null, ...fields }) as StatementDocument;

it("shows unknown progress without inventing a percentage", () => {
  render(<QueryProgress rows={0} document={doc({})} />);
  expect(screen.getByRole("progressbar").hasAttribute("value")).toBe(false);
  expect(screen.queryByText(/estimated/)).toBeNull();
});
it("shows a DuckDB estimate while receiving rows", () => {
  render(<QueryProgress rows={25} receiving document={doc({ progress: { fraction: 0.42 } })} />);
  expect(screen.getByRole("progressbar").getAttribute("value")).toBe("0.42");
  expect(screen.getByText("Receiving results…")).toBeTruthy();
});
