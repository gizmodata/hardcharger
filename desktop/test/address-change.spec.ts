import { test, expect, type Page } from "@playwright/test";

// A server set up for 192.168.4.21 with a certificate generated here, on a computer DHCP has since
// moved to 192.168.4.22.
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const old = { certificate: "/certs/old/server.pem", privateKey: "/certs/old/server.key", trustCertificate: "/certs/old/Hardcharger-local-CA.crt",
      fingerprint: "AA:BB", expires: "2027-10-26", names: ["127.0.0.1", "192.168.4.21", "::1", "localhost"] };
    const certificates = [old];
    const status = { running: false, port: 31337, endpoint: "https://192.168.4.21:31337", version: "Hardcharger 2.3.0", error: null,
      settingsPending: false, pendingNetwork: null as boolean | null, generatedCertificate: old as typeof old | null,
      settings: { port: 31337, start_on_open: true, server_options: {}, network: { enabled: true, listen_address: "0.0.0.0",
        server_name: "192.168.4.21", tls: true, tls_cert: old.certificate, tls_key: old.privateKey, username: "hardcharger",
        password: "__HARDCHARGER_STORED_SECRET__" } } };
    const state = { status, calls: [] as string[], addresses: [{ address: "192.168.4.22", interface: "en0" }] };
    Object.assign(window, { desktopTest: state, isTauri: true, __TAURI_INTERNALS__: {
      transformCallback: () => 1,
      invoke: async (command: string, args: { settings: typeof status.settings; pending: boolean; network: boolean; action: string; serverName: string }) => {
        if (command === "server_options") return [];
        if (command === "network_info") return structuredClone({ hostname: "my-mac.local", addresses: state.addresses });
        if (command === "settings_pending") { status.settingsPending = args.pending; status.pendingNetwork = args.network ?? null; return; }
        if (command === "generate_certificate") {
          state.calls.push(`generate ${args.serverName}`);
          const n = certificates.length;
          const made = { ...old, certificate: `/certs/${n}/server.pem`, privateKey: `/certs/${n}/server.key`, trustCertificate: `/certs/${n}/Hardcharger-local-CA.crt`,
            fingerprint: `CC:${n}`, names: ["127.0.0.1", "::1", "localhost", args.serverName, ...state.addresses.map(a => a.address)] };
          certificates.push(made);
          return made;
        }
        if (command === "save_settings") {
          state.calls.push(`save ${args.settings.network.server_name} ${args.settings.network.listen_address} ${args.settings.network.tls_cert}`);
          status.settings = { ...args.settings, network: { ...args.settings.network, password: "__HARDCHARGER_STORED_SECRET__" } };
          status.settingsPending = false; status.pendingNetwork = null;
          status.generatedCertificate = certificates.find(c => c.certificate === args.settings.network.tls_cert) ?? null;
          status.endpoint = `https://${status.settings.network.server_name}:${status.settings.port}`;
        }
        if (command === "server_control") {
          state.calls.push(args.action);
          if (!state.addresses.some(a => a.address === status.settings.network.server_name))
            throw new Error(`${status.settings.network.server_name} is no longer an address of this computer. It now has 192.168.4.22 (en0). Choose the new address in Settings > Network access.`);
          status.running = args.action !== "stop";
        }
        return ["server_status", "save_settings", "server_control"].includes(command) ? status : false;
      },
    } });
  });
});

const calls = (page: Page) => page.evaluate(() => (window as unknown as { desktopTest: { calls: string[] } }).desktopTest.calls);

test("a server address the computer lost is offered its replacement, with a new certificate, in one click", async ({ page }) => {
  await page.goto("/");
  const notice = page.locator(".address-change");
  await expect(notice).toContainText("The server is set up for 192.168.4.21, which this computer no longer has. It now has 192.168.4.22 (en0).");
  await page.getByRole("button", { name: "Start server", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("192.168.4.21 is no longer an address of this computer");
  await page.getByRole("button", { name: "Use 192.168.4.22", exact: true }).click();
  await expect(page.getByText(/^Now using 192\.168\.4\.22\. A new certificate was generated/)).toBeVisible();
  expect(await calls(page)).toEqual(["start", "generate 192.168.4.22", "save 192.168.4.22 0.0.0.0 /certs/1/server.pem"]);
  await expect(notice).toHaveCount(0);
  await expect(page.locator("#save-state")).toHaveText("Saved settings · changes apply at the next start");
  await expect(page.getByLabel("Server DNS name or IP", { exact: true })).toHaveValue("192.168.4.22");
  await expect(page.getByRole("heading", { name: /Certificate generated/ })).toBeVisible();
  await expect(page.locator("#certificate-coverage")).toBeHidden();
  await page.screenshot({ path: "artifacts/desktop-address-changed.png", fullPage: true });
  await page.getByRole("button", { name: "Local server", exact: true }).click();
  await page.getByRole("button", { name: "Start server", exact: true }).click();
  await expect(page.getByText("RUNNING", { exact: true })).toBeVisible();
  expect((await calls(page)).at(-1)).toBe("start");
});

test("a listen address pinned to the lost address moves with it", async ({ page }) => {
  await page.addInitScript(() => {
    const { status } = (window as unknown as { desktopTest: { status: { settings: { network: { listen_address: string } } } } }).desktopTest;
    status.settings.network.listen_address = "192.168.4.21";
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Use 192.168.4.22", exact: true }).click();
  await expect(page.getByText(/^Now using 192\.168\.4\.22/)).toBeVisible();
  expect((await calls(page)).at(-1)).toBe("save 192.168.4.22 192.168.4.22 /certs/1/server.pem");
});

test("an address change noticed while the app is open is offered too, but not while the server runs", async ({ page }) => {
  await page.clock.install();
  await page.addInitScript(() => {
    const test = (window as unknown as { desktopTest: { addresses: { address: string }[]; status: { running: boolean } } }).desktopTest;
    test.addresses[0].address = "192.168.4.21"; // still on the old address at launch
    test.status.running = true;
  });
  await page.goto("/");
  await expect(page.getByText("RUNNING", { exact: true })).toBeVisible();
  await expect(page.locator(".address-change")).toHaveCount(0);
  await page.evaluate(() => { (window as unknown as { desktopTest: { addresses: { address: string }[] } }).desktopTest.addresses[0].address = "192.168.4.22"; });
  await page.clock.runFor(31_000);
  await expect(page.locator(".address-change")).toContainText("Stop the server to switch.");
  await expect(page.getByRole("button", { name: "Use 192.168.4.22", exact: true })).toBeDisabled();
});

test("editing the address flags a certificate that doesn't cover it, and a saved notice gives way to unsaved changes", async ({ page }) => {
  await page.addInitScript(() => {
    (window as unknown as { desktopTest: { addresses: { address: string }[] } }).desktopTest.addresses.push({ address: "192.168.4.21", interface: "en0" });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.locator(".address-change")).toHaveCount(0); // 192.168.4.21 is still this computer's
  const save = page.getByRole("button", { name: "Save settings", exact: true });
  await save.click();
  const saved = page.getByText("Settings saved. They take effect the next time the server starts.", { exact: true });
  await expect(saved).toBeVisible();
  await page.getByRole("button", { name: "Use 192.168.4.22 en0", exact: true }).click();
  await expect(saved).toHaveCount(0);
  await expect(page.locator("#save-state")).toHaveText("Unsaved changes");
  const coverage = page.locator("#certificate-coverage");
  await expect(coverage).toHaveText("This certificate doesn't cover 192.168.4.22: it was generated for an earlier address. Generate a new certificate, or clients will refuse the connection.");
  await save.click();
  await expect(page.getByRole("alert")).toContainText("This certificate doesn't cover 192.168.4.22");
  await expect(page.getByLabel("TLS certificate chain (PEM path)", { exact: true })).toHaveAttribute("aria-invalid", "true");
  expect((await calls(page)).filter(c => c.startsWith("save"))).toHaveLength(1); // only the first, unchanged save
  await page.getByRole("button", { name: "Generate certificate and key", exact: true }).click();
  await expect(coverage).toBeHidden();
  await save.click();
  await expect(saved).toBeVisible();
  expect((await calls(page)).at(-1)).toBe("save 192.168.4.22 0.0.0.0 /certs/1/server.pem");
});
