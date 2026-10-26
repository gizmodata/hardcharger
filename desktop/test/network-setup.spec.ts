import { test, expect } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const resume = new URL(location.href).searchParams.has("resume");
    const certificate = { certificate: "/test/server.pem", privateKey: "/test/server.key", trustCertificate: "/test/Hardcharger-local-CA.crt",
      fingerprint: "AA:BB", expires: "2027-10-26", names: ["192.168.1.20", "my-mac.local"] };
    const status = { running: false, port: 31337, endpoint: "http://127.0.0.1:31337", version: "Hardcharger 2.0.0", error: null,
      settingsPending: resume, pendingNetwork: (resume ? true : null) as boolean | null, generatedCertificate: null as typeof certificate | null,
      settings: { port: 31337, start_on_open: true, server_options: {}, network: { enabled: false, listen_address: "0.0.0.0",
        server_name: "", tls: true, tls_cert: "", tls_key: "", username: "hardcharger", password: "" } } };
    const controls: string[] = [];
    const state = { controls, failSave: false, status };
    Object.assign(window, { desktopTest: state, isTauri: true, __TAURI_INTERNALS__: {
      transformCallback: () => 1,
      invoke: async (command: string, args: { settings: typeof status.settings; pending: boolean; network: boolean; action: string }) => {
        if (command === "server_options") return [];
        if (command === "network_info") return { hostname: "my-mac.local", addresses: [{ address: "192.168.1.20", interface: "en0" }] };
        if (command === "settings_pending") { status.settingsPending = args.pending; status.pendingNetwork = args.network ?? null; return; }
        if (command === "generate_certificate") return certificate;
        if (command === "save_settings") {
          if (state.failSave) throw new Error("Could not read TLS certificate: file unavailable");
          status.settings = args.settings;
          status.settings.network.password = "__HARDCHARGER_STORED_SECRET__";
          status.settingsPending = false; status.pendingNetwork = null; status.generatedCertificate = certificate;
          status.endpoint = `https://${status.settings.network.server_name}:${status.settings.port}`;
        }
        if (command === "server_control") {
          controls.push(args.action);
          if (status.settingsPending || !status.settings.network.enabled) throw new Error("Unexpected local-mode fallback");
          status.running = true;
        }
        return ["server_status", "save_settings", "server_control"].includes(command) ? status : false;
      },
    } });
  });
});

test("unsaved network setup survives navigation, validates fields, and never starts the old local config", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const save = page.getByRole("button", { name: "Save settings", exact: true });
  await expect(save).toBeInViewport();
  await page.getByLabel("Connections", { exact: true }).selectOption("network");
  await expect(page.getByLabel("Server DNS name or IP", { exact: true })).toHaveValue("192.168.1.20");
  await expect(page.getByLabel("Administrator password", { exact: true })).toHaveAttribute("aria-required", "true");
  await page.getByRole("button", { name: "Local server", exact: true }).click();
  await expect(page.getByText("Settings pending · start blocked until saved")).toBeVisible();
  await page.getByRole("button", { name: "Save settings and start", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Network setup is incomplete");
  await expect(page.getByLabel("Connections", { exact: true })).toHaveValue("network");
  await expect(page.getByLabel("Administrator password", { exact: true })).toHaveAttribute("aria-invalid", "true");
  expect(await page.evaluate(() => (window as unknown as { desktopTest: { controls: string[] } }).desktopTest.controls)).toEqual([]);
  await page.getByRole("button", { name: "Generate certificate and key", exact: true }).click();
  await expect(page.getByLabel("TLS certificate chain (PEM path)", { exact: true })).toHaveValue("/test/server.pem");
  await expect(page.getByLabel("TLS private key (PEM path)", { exact: true })).toHaveValue("/test/server.key");
  await expect(page.getByRole("heading", { name: /Certificate generated/ })).toBeVisible();
  await page.getByLabel("Administrator password", { exact: true }).fill("synthetic-network-setup-password");
  await page.getByLabel("Connections", { exact: true }).scrollIntoViewIfNeeded();
  await expect(save).toBeInViewport(); // stays visible while settings scroll
  await page.screenshot({ path: "artifacts/desktop-network-setup.png", fullPage: true });
  await page.evaluate(() => { (window as unknown as { desktopTest: { failSave: boolean } }).desktopTest.failSave = true; });
  await save.click();
  await expect(page.getByRole("alert")).toContainText("Could not read TLS certificate");
  await expect(page.getByLabel("Administrator password", { exact: true })).toHaveValue("synthetic-network-setup-password");
  await page.getByRole("button", { name: "Local server", exact: true }).click();
  await page.getByRole("button", { name: "Save settings and start", exact: true }).click();
  expect(await page.evaluate(() => (window as unknown as { desktopTest: { controls: string[] } }).desktopTest.controls)).toEqual([]);
  await page.evaluate(() => { (window as unknown as { desktopTest: { failSave: boolean } }).desktopTest.failSave = false; });
  await page.getByRole("button", { name: "Local server", exact: true }).click();
  await page.getByRole("button", { name: "Save settings and start", exact: true }).click();
  await expect(page.getByText("Network access · TLS", { exact: true })).toBeVisible();
  await expect(page.getByText("https://192.168.1.20:31337/ui/", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => (window as unknown as { desktopTest: { controls: string[] } }).desktopTest.controls)).toEqual(["start"]);
});

test("discarding setup is explicit and removes its pending start guard", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByLabel("Connections", { exact: true }).selectOption("network");
  await page.getByRole("button", { name: "About Hardcharger", exact: true }).click();
  await expect(page.getByText("Unsaved server settings", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Discard changes", exact: true }).click();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByLabel("Connections", { exact: true })).toHaveValue("local");
  await expect(page.getByText("Unsaved server settings", { exact: true })).toBeHidden();
});

test("a pending network setup from a previous launch cannot start the saved local configuration", async ({ page }) => {
  await page.goto("/?resume=1");
  await expect(page.getByText("Unsaved server settings", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Start server", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("previous setup was not saved");
  await expect(page.getByLabel("Connections", { exact: true })).toHaveValue("network");
  expect(await page.evaluate(() => (window as unknown as { desktopTest: { controls: string[] } }).desktopTest.controls)).toEqual([]);
});
