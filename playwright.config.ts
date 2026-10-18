// End-to-end tests: the built UI in Chromium and WebKit against a real hardcharger-server
// (with access control on, so a non-admin user's view is tested), served the way the server
// serves it (scripts/serve.mjs, same origin as Warp SQL). test/e2e/global-setup.ts starts
// both and fails the run when it can't: these tests never skip.
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "test/e2e",
  // *.embedded.spec.ts need the UI served by the server itself (HARDCHARGER_UI_TEST_EMBEDDED=1,
  // a server build that embeds it): mutual TLS can't be proxied, since a client certificate
  // is presented to the server that binds tokens to it.
  testMatch: process.env["HARDCHARGER_UI_TEST_EMBEDDED"] === "1" ? "*.spec.ts" : "!(*.embedded).spec.ts",
  globalSetup: "./test/e2e/global-setup.ts",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  forbidOnly: true,
  retries: 0,
  // One test at a time: the WebKit and Chromium copy tests share the system clipboard on
  // macOS, and the server is shared.
  workers: 1,
  fullyParallel: false,
  reporter: process.env["CI"] ? [["list"], ["html", { open: "never" }]] : [["list"]],
  use: {
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    viewport: { width: 1360, height: 860 },
  },
  // The browsers' own user agents (no device presets): the editor reads the platform from
  // it, and the tests' ControlOrMeta must mean the same key it does.
  projects: [
    { name: "chromium", use: { browserName: "chromium" } },
    { name: "webkit", use: { browserName: "webkit" } },
  ],
});
