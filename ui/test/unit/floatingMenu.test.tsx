import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { FloatingMenu } from "../../src/components/FloatingMenu";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
it("keeps an overflowing toolbar's menu inside a phone viewport", () => {
  vi.spyOn(document.documentElement, "clientWidth", "get").mockReturnValue(412);
  vi.spyOn(document.documentElement, "clientHeight", "get").mockReturnValue(647);
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(220);
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(260);
  render(<FloatingMenu x={510} y={600} align="right" items={[{ label: "CSV" }]} onClose={() => {}} />);
  const menu = screen.getByRole("menu");
  expect(menu.style.left).toBe("184px");
  expect(menu.style.top).toBe("379px");
});
