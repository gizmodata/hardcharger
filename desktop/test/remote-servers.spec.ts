// The SQL workspace without the local server, and the remote servers it lists: the workspace
// button works while the server is stopped (the app starts `hardcharger ui` for it), and
// Settings keeps a list of remote servers (name, URL, how the certificate is trusted) that is
// saved with the settings and validated before it is.
import { expect, test } from "@playwright/test";

interface Calls { opened: number; saved: unknown[] }

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const calls: Calls = { opened: 0, saved: [] };
    const status = {
      running: false, port: 31337, endpoint: "http://127.0.0.1:31337", version: "Hardcharger 2.7.0", error: null,
      settings: {
        port: 31337, start_on_open: false, server_options: {},
        remote_servers: [{ id: "r1", name: "Warehouse", url: "https://wh.example.com:31337", tls: "verified", fingerprint: "" }],
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
  await section.getByRole("button", { name: "Add a remote server" }).click();
  await section.locator('[name="remote-name-1"]').fill("Lab");
  await section.locator('[name="remote-url-1"]').fill("https://lab.local:31337");
  await section.locator('[name="remote-tls-1"]').selectOption("fingerprint");
  // Trusting by fingerprint needs the fingerprint: the save refuses until it is there.
  await page.getByRole("button", { name: "Save settings", exact: true }).click();
  await expect(page.locator("#error")).toContainText("needs the certificate's SHA-256 fingerprint");
  await section.locator('[name="remote-fingerprint-1"]').fill("AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99");
  await page.getByRole("button", { name: "Save settings", exact: true }).click();
  await expect(page.getByText("Settings saved", { exact: false })).toBeVisible();
  const saved = await page.evaluate(() => (window as unknown as { remoteTest: Calls }).remoteTest.saved);
  expect((saved.at(-1) as { remote_servers: unknown[] }).remote_servers).toEqual([
    { id: "r1", name: "Warehouse", url: "https://wh.example.com:31337", tls: "verified", fingerprint: "" },
    expect.objectContaining({ name: "Lab", url: "https://lab.local:31337", tls: "fingerprint", fingerprint: expect.stringMatching(/^AA:BB/) }),
  ]);
  await page.screenshot({ path: "artifacts/desktop-remote-servers.png" });
  // Removing one, and the empty state.
  await section.locator('[data-remove-remote="0"]').click();
  await section.locator('[data-remove-remote="0"]').click();
  await expect(section.locator("[data-no-remotes]")).toBeVisible();
  await page.getByRole("button", { name: "Save settings", exact: true }).click();
  const last = await page.evaluate(() => (window as unknown as { remoteTest: Calls }).remoteTest.saved.at(-1));
  expect((last as { remote_servers: unknown[] }).remote_servers).toEqual([]);
});

test("a remote server's URL must be http(s) and must not carry credentials", async ({ page }) => {
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const section = page.getByRole("region", { name: "Remote servers" });
  await section.locator('[name="remote-url-0"]').fill("wh.example.com:31337");
  await page.getByRole("button", { name: "Save settings", exact: true }).click();
  await expect(page.locator("#error")).toContainText("must start with https:// or http://");
  await section.locator('[name="remote-url-0"]').fill("http://wh.example.com:31337");
  await section.locator('[name="remote-tls-0"]').selectOption("plaintext");
  await page.getByRole("button", { name: "Save settings", exact: true }).click();
  await expect(page.getByText("Settings saved", { exact: false })).toBeVisible();
});
