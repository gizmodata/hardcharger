// The SQL workspace without the local server, and the remote servers it lists: the workspace
// button works while the server is stopped (the app starts `hardcharger ui` for it), and
// Settings keeps a list of remote servers with the workspace dialog's fields (display name,
// host, port, Use TLS, Trust this server's certificate), saved with the settings and validated
// before it is. Never a password: the workspace keeps those in the keychain.
import { expect, test } from "@playwright/test";

interface Calls { opened: number; saved: unknown[] }

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const calls: Calls = { opened: 0, saved: [] };
    const status = {
      running: false, port: 31337, endpoint: "http://127.0.0.1:31337", version: "Hardcharger 2.7.0", error: null,
      settings: {
        port: 31337, start_on_open: false, server_options: {},
        remote_servers: [{ id: "r1", name: "Warehouse", host: "wh.example.com", port: 31337, tls: true, prefix: "", trust: false, fingerprint: "", username: "ann", timeout: 30 }],
      },
    };
    Object.assign(window, { isTauri: true, remoteTest: calls, __TAURI_INTERNALS__: {
      transformCallback: () => 1,
      invoke: async (command: string, args: { settings?: { remote_servers?: unknown[]; port?: number } }) => {
        if (command === "server_options") return [];
        if (command === "server_status") return status;
        if (command === "open_workspace") { calls.opened++; return; }
        if (command === "settings_pending") return;
        if (command === "save_settings") {
          calls.saved.push(args.settings);
          status.settings = { ...status.settings, ...args.settings } as typeof status.settings;
          return status;
        }
        return false;
      },
    } });
  });
  await page.goto("/");
});

test("Open SQL workspace is offered while the server is stopped, and says what it opens", async ({ page }) => {
  const hero = page.getByRole("button", { name: "Open SQL workspace", exact: true });
  await expect(hero).toBeEnabled();
  await expect(page.getByText("Start server", { exact: true })).toBeVisible();
  const nav = page.getByRole("button", { name: /^SQL workspace/ });
  await expect(nav).toBeEnabled();
  await expect(nav).toHaveAttribute("title", /remote servers.*stopped/);
  await hero.click();
  await expect.poll(() => page.evaluate(() => (window as unknown as { remoteTest: Calls }).remoteTest.opened)).toBe(1);
  await page.screenshot({ path: "artifacts/desktop-workspace-stopped.png" });
});

test("remote servers are listed, added, validated, removed and saved with the settings", async ({ page }) => {
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const section = page.getByRole("region", { name: "Remote servers" });
  await expect(section.locator('[name="remote-name-0"]')).toHaveValue("Warehouse");
  await expect(section.locator('[name="remote-host-0"]')).toHaveValue("wh.example.com");
  await expect(section.locator('[name="remote-port-0"]')).toHaveValue("31337");
  await expect(section.locator('[name="remote-tls-0"]')).toBeChecked();
  await expect(section.locator('[name="remote-trust-0"]')).not.toBeChecked();
  await expect(section.locator(".remote-lock").first()).toHaveText("Verified");
  await section.getByRole("button", { name: "Add a remote server" }).click();
  await section.locator('[name="remote-name-1"]').fill("Lab");
  await section.locator('[name="remote-host-1"]').fill("lab.local");
  await expect(section.locator('[name="remote-port-1"]')).toHaveValue("31337");
  await section.locator('[name="remote-trust-1"]').check();
  await expect(section.locator(".remote-lock").nth(1)).toHaveText("Trusted by fingerprint");
  // A fingerprint is optional, but must be a SHA-256 when given.
  await section.locator('[name="remote-fingerprint-1"]').fill("zz");
  await page.getByRole("button", { name: "Save settings", exact: true }).click();
  await expect(page.locator("#error")).toContainText("SHA-256");
  await section.locator('[name="remote-fingerprint-1"]').fill("AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99");
  await page.getByRole("button", { name: "Save settings", exact: true }).click();
  await expect(page.getByText("Settings saved", { exact: false })).toBeVisible();
  const saved = await page.evaluate(() => (window as unknown as { remoteTest: Calls }).remoteTest.saved);
  expect((saved.at(-1) as { remote_servers: unknown[] }).remote_servers).toEqual([
    { id: "r1", name: "Warehouse", host: "wh.example.com", port: 31337, tls: true, prefix: "", trust: false, fingerprint: "", username: "ann", timeout: 30 },
    expect.objectContaining({ name: "Lab", host: "lab.local", port: 31337, tls: true, trust: true, fingerprint: expect.stringMatching(/^AA:BB/) }),
  ]);
  expect(JSON.stringify((saved.at(-1) as { remote_servers: unknown[] }).remote_servers)).not.toMatch(/password/i);
  await page.screenshot({ path: "artifacts/desktop-remote-servers.png" });
  // Use TLS off turns Trust off too (plain HTTP has no certificate).
  await section.locator('[name="remote-tls-1"]').uncheck();
  await expect(section.locator('[name="remote-trust-1"]')).toBeDisabled();
  await expect(section.locator(".remote-lock").nth(1)).toHaveText("Not encrypted");
  // Removing them, and the empty state.
  await section.locator('[data-remove-remote="0"]').click();
  await section.locator('[data-remove-remote="0"]').click();
  await expect(section.locator("[data-no-remotes]")).toBeVisible();
  await page.getByRole("button", { name: "Save settings", exact: true }).click();
  const last = await page.evaluate(() => (window as unknown as { remoteTest: Calls }).remoteTest.saved.at(-1));
  expect((last as { remote_servers: unknown[] }).remote_servers).toEqual([]);
});

test("a remote server's host must be a host and its port a port", async ({ page }) => {
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const section = page.getByRole("region", { name: "Remote servers" });
  await section.locator('[name="remote-host-0"]').fill("https://wh.example.com");
  await page.getByRole("button", { name: "Save settings", exact: true }).click();
  await expect(page.locator("#error")).toContainText("without a scheme");
  await section.locator('[name="remote-host-0"]').fill("wh.example.com");
  await section.locator('[name="remote-port-0"]').fill("70000");
  await page.getByRole("button", { name: "Save settings", exact: true }).click();
  await expect(page.locator("#error")).toContainText("port");
  await section.locator('[name="remote-port-0"]').fill("8443");
  await section.locator('[name="remote-tls-0"]').uncheck();
  await page.getByRole("button", { name: "Save settings", exact: true }).click();
  await expect(page.getByText("Settings saved", { exact: false })).toBeVisible();
  const last = await page.evaluate(() => (window as unknown as { remoteTest: Calls }).remoteTest.saved.at(-1));
  expect((last as { remote_servers: unknown[] }).remote_servers).toEqual([
    expect.objectContaining({ host: "wh.example.com", port: 8443, tls: false, trust: false }),
  ]);
});
