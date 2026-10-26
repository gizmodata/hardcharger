import { test, expect } from "@playwright/test";
import { defaultNetwork, validateNetwork } from "../src/network";

test("network settings require an address, credentials, and complete TLS configuration", () => {
  expect(() => validateNetwork(defaultNetwork)).not.toThrow();
  const network = { ...defaultNetwork, enabled: true };
  expect(() => validateNetwork(network)).toThrow(/DNS name or IP/);
  network.server_name = "desktop.test";
  expect(() => validateNetwork(network)).toThrow(/password/);
  network.password = "synthetic-network-test-password";
  expect(() => validateNetwork(network)).toThrow(/certificate chain and private key/);
  network.tls_cert = "/test/server.pem";
  network.tls_key = "/test/server.key";
  expect(() => validateNetwork(network)).not.toThrow();
  for (const server_name of ["http://desktop.test", "user@desktop.test", "desktop.test:31337", "0.0.0.0"])
    expect(() => validateNetwork({ ...network, server_name })).toThrow();
});

test("network controls keep local access as the default and preserve edits on validation errors", async ({ page }) => {
  const failures: string[] = [];
  page.on("pageerror", e => failures.push(e.message));
  await page.addInitScript(() => {
    const status = { running: false, port: 31337, endpoint: "http://127.0.0.1:31337", version: "Hardcharger 2.0.0", error: null,
      settings: { port: 31337, start_on_open: true, server_options: {}, network: {
        enabled: false, listen_address: "0.0.0.0", server_name: "", tls: true,
        tls_cert: "", tls_key: "", username: "hardcharger", password: "",
      } } };
    Object.assign(window, { isTauri: true, __TAURI_INTERNALS__: {
      transformCallback: () => 1,
      invoke: async (command: string, args: { settings: typeof status.settings }) => {
        if (command === "server_options") return [];
        if (command === "save_settings") {
          status.settings = args.settings;
          status.settings.network.password = "__HARDCHARGER_STORED_SECRET__";
          status.endpoint = `https://${status.settings.network.server_name}:${status.settings.port}`;
        }
        return command === "server_status" || command === "save_settings" ? status : false;
      },
    } });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByLabel("Connections", { exact: true })).toHaveValue("local");
  await expect(page.getByLabel("Listen IP address", { exact: true })).toBeHidden();
  await page.getByLabel("Connections", { exact: true }).selectOption("network");
  await expect(page.getByLabel("Transport", { exact: true })).toHaveValue("tls");
  await page.getByLabel("Server DNS name or IP", { exact: true }).fill("my-mac.local");
  await page.getByLabel("Administrator password", { exact: true }).fill("synthetic-network-test-password");
  await page.getByRole("button", { name: "Save settings", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("TLS requires both");
  await expect(page.getByLabel("Administrator password", { exact: true })).toHaveValue("synthetic-network-test-password");
  await expect(page.getByLabel("Server DNS name or IP", { exact: true })).toHaveValue("my-mac.local");
  await page.getByLabel("TLS certificate chain (PEM path)", { exact: true }).fill("/test/server.pem");
  await page.getByLabel("TLS private key (PEM path)", { exact: true }).fill("/test/server.key");
  await page.getByRole("button", { name: "Save settings", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Settings saved" })).toBeVisible();
  await expect(page.getByLabel("Administrator password", { exact: true })).toHaveValue("");
  await page.getByLabel("Transport", { exact: true }).selectOption("http");
  await expect(page.getByText("HTTP sends passwords, queries, and results without encryption.", { exact: false })).toBeVisible();
  await expect(page.getByLabel("TLS private key (PEM path)", { exact: true })).toBeHidden();
  await page.getByLabel("Transport", { exact: true }).selectOption("tls");
  await page.getByLabel("Connections", { exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: "artifacts/desktop-network-settings.png", fullPage: true });
  await page.getByRole("button", { name: "Local server", exact: true }).click();
  await expect(page.getByText("Connect from another computer", { exact: true })).toBeVisible();
  await expect(page.getByText("https://my-mac.local:31337/ui/", { exact: true })).toBeVisible();
  expect(failures).toEqual([]);
});
