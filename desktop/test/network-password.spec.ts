import { test, expect, type Page } from "@playwright/test";

// The network-mode administrator password follows 2.14.0's password policy: at least 12
// characters. A new one shorter than that is refused in the form before anything is saved; one
// saved before 2.14.0 keeps working, and the app asks for a new one.

interface DesktopTest { saved: { network: { password: string } }[]; controls: string[] }

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const params = new URL(location.href).searchParams;
    const short = params.has("short");
    const certificate = { certificate: "/test/server.pem", privateKey: "/test/server.key", trustCertificate: "/test/Hardcharger-local-CA.crt",
      fingerprint: "AA:BB", expires: "2027-10-26", names: ["192.168.1.20"] };
    const status = { running: false, port: 31337, endpoint: "http://127.0.0.1:31337", version: "Hardcharger 2.14.0", error: null,
      settingsPending: false, pendingNetwork: null as boolean | null, generatedCertificate: (short ? certificate : null) as typeof certificate | null,
      networkPasswordShort: short,
      settings: { port: 31337, start_on_open: true, server_options: {}, network: short
        ? { enabled: true, listen_address: "0.0.0.0", server_name: "192.168.1.20", tls: true, tls_cert: "/test/server.pem",
          tls_key: "/test/server.key", username: "hardcharger", password: "__HARDCHARGER_STORED_SECRET__" }
        : { enabled: false, listen_address: "0.0.0.0", server_name: "", tls: true, tls_cert: "", tls_key: "", username: "hardcharger", password: "" } } };
    const state = { saved: [] as unknown[], controls: [] as string[], status };
    Object.assign(window, { desktopTest: state, isTauri: true, __TAURI_INTERNALS__: {
      transformCallback: () => 1,
      invoke: async (command: string, args: { settings: typeof status.settings; pending: boolean; network: boolean; action: string }) => {
        if (command === "server_options") return [];
        if (command === "network_info") return { hostname: "my-mac.local", addresses: [{ address: "192.168.1.20", interface: "en0" }] };
        if (command === "settings_pending") { status.settingsPending = args.pending; status.pendingNetwork = args.network ?? null; return; }
        if (command === "generate_certificate") return certificate;
        if (command === "save_settings") {
          state.saved.push(JSON.parse(JSON.stringify(args.settings)));
          // As the app does: a typed password replaces the saved one; the kept one stays as it was.
          if (args.settings.network.password !== "__HARDCHARGER_STORED_SECRET__") status.networkPasswordShort = false;
          status.settings = args.settings;
          status.settings.network.password = "__HARDCHARGER_STORED_SECRET__";
          status.settingsPending = false; status.pendingNetwork = null; status.generatedCertificate = certificate;
        }
        if (command === "server_control") { state.controls.push(args.action); status.running = args.action !== "stop"; }
        return ["server_status", "save_settings", "server_control"].includes(command) ? status : false;
      },
    } });
  });
});

const desktop = (page: Page) => page.evaluate(() => (window as unknown as { desktopTest: DesktopTest }).desktopTest);

async function setUpNetwork(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByLabel("Connections", { exact: true }).selectOption("network");
  await page.getByRole("button", { name: "Generate certificate and key", exact: true }).click();
  await expect(page.getByLabel("TLS certificate chain (PEM path)", { exact: true })).toHaveValue("/test/server.pem");
}

test("a new administrator password under 12 characters is refused before saving; 12 saves", async ({ page }) => {
  await setUpNetwork(page);
  const password = page.getByLabel("Administrator password", { exact: true });
  const save = page.getByRole("button", { name: "Save settings", exact: true });
  await expect(password).toHaveAttribute("minlength", "12");
  await expect(page.getByText(/At least 12 characters\. Stored in the OS credential store/)).toBeVisible();
  // 11 characters; then 11 characters that take 22 and 44 bytes (the policy counts characters).
  for (const short of ["eleven-char", "ééééééééééé", "🔑🔑🔑🔑🔑🔑🔑🔑🔑🔑🔑"]) {
    await password.fill(short);
    await save.click();
    await expect(page.getByRole("alert")).toHaveText("Error: Network setup is incomplete. The administrator password needs at least 12 characters.");
    await expect(password).toHaveAttribute("aria-invalid", "true");
    await expect(page.locator("#network-password-error")).toHaveText("The administrator password needs at least 12 characters.");
    await expect(password).toHaveValue(short); // what was typed stays to fix
  }
  expect((await desktop(page)).saved).toEqual([]);
  await password.fill("🔑🔑🔑🔑🔑🔑🔑🔑🔑🔑🔑🔑");
  await save.click();
  await expect(page.getByText("Saved settings · changes apply at the next start", { exact: true })).toBeVisible();
  await expect(password).toHaveAttribute("aria-invalid", "false");
  const saved = (await desktop(page)).saved;
  expect(saved.map(s => s.network.password)).toEqual(["🔑🔑🔑🔑🔑🔑🔑🔑🔑🔑🔑🔑"]);
  await expect(page.locator(".password-short")).toHaveCount(0);
});

test("a saved password under 12 characters keeps working, and the app asks for a new one", async ({ page }) => {
  await page.goto("/?short=1");
  const notice = page.locator(".password-short");
  await expect(notice).toContainText("Change the administrator password");
  await expect(notice).toContainText("shorter than 12 characters");
  // Nothing stops: the saved setup starts as before.
  await page.getByRole("button", { name: "Start server", exact: true }).click();
  await expect(page.getByText("RUNNING", { exact: true })).toBeVisible();
  expect((await desktop(page)).controls).toEqual(["start"]);
  await expect(notice).toContainText("Stop the server to change it.");
  await page.getByRole("button", { name: "Stop", exact: true }).click();
  await page.getByRole("button", { name: "Stop server", exact: true }).click();
  await expect(page.getByText("STOPPED", { exact: true })).toBeVisible();
  // Change password opens Settings at the password, with the same warning beside it.
  await page.getByRole("button", { name: "Change password", exact: true }).click();
  const password = page.getByLabel("Administrator password", { exact: true });
  await expect(password).toBeFocused();
  await expect(page.locator("#network-password-short")).toContainText("It still works; type a new one here and save to change it.");
  // Saving other settings with the password left blank keeps the saved one (no refusal).
  await page.locator('input[name="port"]').fill("31338");
  await page.getByRole("button", { name: "Save settings", exact: true }).click();
  await expect(page.getByText("Saved settings · changes apply at the next start", { exact: true })).toBeVisible();
  expect((await desktop(page)).saved.map(s => s.network.password)).toEqual(["__HARDCHARGER_STORED_SECRET__"]);
  await expect(page.locator("#network-password-short")).toBeVisible();
  // Another short one is refused; a long one replaces it and the warning goes.
  await password.fill("still-short");
  await page.getByRole("button", { name: "Save settings", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveText("Error: Network setup is incomplete. The administrator password needs at least 12 characters.");
  expect((await desktop(page)).saved).toHaveLength(1);
  await password.fill("a-much-longer-password");
  await page.getByRole("button", { name: "Save settings", exact: true }).click();
  await expect(page.getByText("Saved settings · changes apply at the next start", { exact: true })).toBeVisible();
  expect((await desktop(page)).saved.map(s => s.network.password)).toEqual(["__HARDCHARGER_STORED_SECRET__", "a-much-longer-password"]);
  await expect(page.locator("#network-password-short")).toHaveCount(0);
  await expect(notice).toHaveCount(0);
});
