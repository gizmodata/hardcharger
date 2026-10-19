import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { CopyrightNotice, TrademarkNotice, Wordmark } from "../../src/components/Brand";

afterEach(() => cleanup());

describe("Brand", () => {
  it("the copyright notice carries the build's year (vitest.config.ts sets it to 1999)", () => {
    render(<CopyrightNotice />);
    expect(screen.getByText("© 1999 GizmoData LLC. All rights reserved.")).toBeTruthy();
  });

  it("the trademark notice names both marks", () => {
    render(<TrademarkNotice />);
    expect(screen.getByText("Hardcharger™ and GizmoData™ are trademarks of GizmoData LLC.")).toBeTruthy();
  });

  it("About's logo is the stacked mascot lockup, a light and a dark variant", () => {
    const { container } = render(<Wordmark size="about" />);
    const images = [...container.querySelectorAll("img")];
    expect(images.map((i) => [i.className, i.alt, i.getAttribute("src")?.replace(/^.*\//, "")])).toEqual([
      ["logo-light", "Hardcharger™", "mascot-stacked-light.webp"],
      ["logo-dark", "Hardcharger™", "mascot-stacked-dark.webp"],
    ]);
    expect(images.map((i) => [i.width, i.height])).toEqual([
      [180, 156],
      [180, 156],
    ]);
  });
});
