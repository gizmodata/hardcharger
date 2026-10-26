import { test, expect } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const certificate = { certificate: "/test/server.pem", privateKey: "/test/server.key", trustCertificate: "/test/Hardcharger-local-CA.crt",
      fingerprint: "AA:BB:CC:DD", expires: "2027-10-26", names: ["192.168.1.20"] };
    const state = { trusted: false, cancelled: false, connectionFailure: false, opened: 0, imports: [] as unknown[] };
    const status = { running: true, port: 31337, endpoint: "https://192.168.1.20:31337", version: "Hardcharger 2.0.0", error: null,
      generatedCertificate: certificate, settings: { port: 31337, start_on_open: true, server_options: {}, network: { enabled: true,
        listen_address: "0.0.0.0", server_name: "192.168.1.20", tls: true, tls_cert: certificate.certificate,
        tls_key: certificate.privateKey, username: "hardcharger", password: "__HARDCHARGER_STORED_SECRET__" } } };
    Object.assign(window, { workspaceTest: state, isTauri: true, __TAURI_INTERNALS__: {
      transformCallback: () => 1,
      invoke: async (command: string, args: unknown) => {
        if (command === "server_options") return [];
        if (command === "server_status") return status;
        if (command === "open_workspace") {
          if (state.connectionFailure) throw "Could not reach the server's SQL workspace";
          if (!state.trusted) throw "WORKSPACE_CA_TRUST_REQUIRED: Certificate trust is required";
          ++state.opened; return;
        }
        if (command === "workspace_trust") return { certificate, serverName: "192.168.1.20", platform: "macos" };
        if (command === "trust_workspace_ca") {
          state.imports.push(args);
          if (state.cancelled) throw "macOS did not approve certificate trust";
          state.trusted = true; return;
        }
        return false;
      },
    } });
  });
});

test("opening the workspace offers CA approval and cancellation leaves trust unchanged", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Open SQL workspace", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Trust Hardcharger certificate" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("AA:BB:CC:DD");
  await expect(dialog).toContainText("192.168.1.20");
  await expect(dialog).toContainText("your user account");
  await page.screenshot({ path: "artifacts/desktop-workspace-ca-trust.png" });
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { workspaceTest: { imports: unknown[] } }).workspaceTest.imports)).toEqual([]);
});

test("explicit CA approval retries the workspace with the reviewed identity", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Open SQL workspace", exact: true }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Trust CA and open workspace", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { workspaceTest: { imports: unknown[]; opened: number } }).workspaceTest)).toMatchObject({
    imports: [{ fingerprint: "AA:BB:CC:DD", serverName: "192.168.1.20" }], opened: 1,
  });
});

test("OS refusal stays actionable and never opens an unverified workspace", async ({ page }) => {
  await page.goto("/");
  await page.evaluate(() => { (window as unknown as { workspaceTest: { cancelled: boolean } }).workspaceTest.cancelled = true; });
  await page.getByRole("button", { name: "Open SQL workspace", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "Trust CA and open workspace", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("macOS did not approve");
  await dialog.getByRole("button", { name: "I already trusted it — retry", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("CA is not trusted yet");
  expect(await page.evaluate(() => (window as unknown as { workspaceTest: { opened: number } }).workspaceTest.opened)).toBe(0);
});

test("ordinary connection failures do not offer to change system trust", async ({ page }) => {
  await page.goto("/");
  await page.evaluate(() => { (window as unknown as { workspaceTest: { connectionFailure: boolean } }).workspaceTest.connectionFailure = true; });
  await page.getByRole("button", { name: "Open SQL workspace", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Could not reach");
  await expect(page.getByRole("dialog")).toHaveCount(0);
});
