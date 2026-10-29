// End-to-end tests: the built UI in Chromium and WebKit against a real hardcharger-server
// (with access control on, so a non-admin user's view is tested), served the way the server
// serves it (scripts/serve.mjs, same origin as Chargewire). test/e2e/global-setup.ts starts
// both and fails the run when it can't: these tests never skip.
import { defineConfig, devices } from "@playwright/test";

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
  // The editor reads the platform from the user agent, and the tests' ControlOrMeta must mean
  // the key it does: Control everywhere but macOS. Chromium's own user agent names the host's
  // platform. Playwright's WebKit claims to be Safari on a Mac on every host, so off macOS it
  // gets that user agent with the host's platform in it (as GNOME Web's), or the editor would
  // wait for ⌘ while the tests press Ctrl.
  projects: [
    { name: "chromium", use: { browserName: "chromium" } },
    {
      name: "webkit",
      use: {
        browserName: "webkit",
        ...(process.platform === "darwin"
          ? {}
          : { userAgent: devices["Desktop Safari"].userAgent.replace(/\(Macintosh;[^)]*\)/, "(X11; Linux x86_64)") }),
      },
    },
  ],
});
