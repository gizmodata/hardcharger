import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { QueryDetailsLink } from "../../src/components/QueryDetailsLink";
import { SessionContext, type AppSession } from "../../src/session";

afterEach(cleanup);
function link(
  options: {
    isAdmin?: boolean;
    fullAdmin?: boolean;
    instrumentationEnabled?: boolean;
    executionId?: string;
    server?: string;
  } = {},
) {
  const {
    isAdmin = true,
    fullAdmin = true,
    instrumentationEnabled = true,
    executionId = "q_example",
    server = "",
  } = options;
  return render(
    <SessionContext value={{ isAdmin, identity: { fullAdmin, instrumentationEnabled } } as AppSession}>
      <QueryDetailsLink executionId={executionId} server={server} />
    </SessionContext>,
  );
}
it("links to the exact execution and preserves sign-in in an independent window", () => {
  link();
  const action = screen.getByRole("link", { name: "SQL details" });
  expect(action.getAttribute("href")).toMatch(/monitor\/query\?execution=q_example$/);
  expect(action.getAttribute("target")).toBe("hardcharger-query-q_example");
  expect(action.getAttribute("rel")).toBe("opener");
});
it.each([{ isAdmin: false }, { fullAdmin: false }, { instrumentationEnabled: false }, { executionId: "" }])(
  "hides unavailable local details: %j",
  (options) => {
    link(options);
    expect(screen.queryByRole("link")).toBeNull();
  },
);
it("keeps remote details scoped to the remote, whose server checks permissions", () => {
  link({ isAdmin: false, instrumentationEnabled: false, server: "remote&id=other" });
  expect(screen.getByRole("link").getAttribute("href")).toMatch(/&server=remote%26id%3Dother$/);
});
