import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ErrorBoundary } from "../../src/components/ErrorBoundary";
import { StaleBuildError } from "../../src/lib/stale";

function Throws({ error }: { error: unknown }): never {
  throw error;
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  sessionStorage.clear();
});

describe("ErrorBoundary", () => {
  it("shows a stale build's recovery instead of the page", () => {
    vi.useFakeTimers();
    render(
      <ErrorBoundary scope="page">
        <Throws error={new StaleBuildError(new TypeError("Importing a module script failed."))} />
      </ErrorBoundary>,
    );
    expect(screen.getByTestId("stale-build").textContent).toContain("Loading the latest version");
    expect(sessionStorage.getItem("hardcharger-ui:stale-reloads")).toContain('"count":1');
  });

  it("shows other errors with a way to try again", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <ErrorBoundary scope="page">
        <Throws error={new Error("kaput")} />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert").textContent).toContain("kaput");
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
  });
});
