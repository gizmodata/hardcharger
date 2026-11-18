// #56: Only this computer's administrator credentials on the dashboard. The password comes from
// the native side only on Reveal or Copy, never with the status, the examples or Export YAML.
import { readFileSync } from "node:fs";
import { expect, test, type Page } from "@playwright/test";

const first = "Abcde-23456-fghij-KLMNP";
const second = "Zyxwv-98765-utsrq-PQRST";

async function mockApp(page: Page, running = true) {
  await page.addInitScript(([initial, next, isRunning]) => {
    const calls: { command: string; args: unknown }[] = [];
    let password = initial;
    const status = () => ({ running: isRunning, pid: isRunning ? 4242 : null, port: 31337, endpoint: "http://127.0.0.1:31337", version: "Hardcharger 2.14.0", error: null,
      database: "/tmp/Hardcharger.duckdb", logFile: "server.log", uptimeSeconds: 5, databaseBytes: 1024,
      settings: { port: 31337, start_on_open: true, server_options: { memory_limit: "1GB" }, network: { enabled: false, tls: true, username: "hardcharger", password: "", server_name: "", listen_address: "0.0.0.0", tls_cert: "", tls_key: "" } },
      localAdmin: { username: "hardcharger", error: null } });
    Object.assign(window, {
      __calls: calls,
      isTauri: true,
      __TAURI_INTERNALS__: {
        transformCallback: () => 1,
        invoke: async (command: string, args: unknown) => {
          calls.push({ command, args });
          if (command === "server_status") return status();
          if (command === "local_admin_password") return password;
          if (command === "regenerate_local_password") { password = next; return status(); }
          if (command === "server_options") return [{ name: "memory_limit", type: "string", group: "Resources", description: "memory", default: "" }];
          return false;
        },
      },
    });
  }, [first, second, running] as const);
  await page.goto("/");
}

const calls = (page: Page) => page.evaluate(() => (window as unknown as { __calls: { command: string; args: unknown }[] }).__calls.map(c => c.command));

async function pasted(page: Page): Promise<string> {
  await page.evaluate(() => { const input = document.createElement("textarea"); input.id = "clipboard-proof"; document.body.append(input); });
  const proof = page.locator("#clipboard-proof");
  await proof.focus();
  await page.keyboard.press("ControlOrMeta+V");
  const text = await proof.inputValue();
  await proof.evaluate(el => el.remove());
  return text;
}

test("the dashboard shows the local administrator, and the password only on Reveal", async ({ page }) => {
  await mockApp(page);
  const card = page.getByRole("region", { name: "Administrator credentials", exact: true });
  await expect(card.getByTestId("local-admin-username")).toHaveText("hardcharger");
  await expect(card.getByTestId("local-admin-password")).toHaveText("•".repeat(16));
  expect(await calls(page)).not.toContain("local_admin_password");
  await expect(page.locator("body")).not.toContainText(first);
  await card.getByRole("button", { name: "Reveal", exact: true }).click();
  await expect(card.getByTestId("local-admin-password")).toHaveText(first);
  await card.screenshot({ path: "artifacts/desktop-local-admin-revealed.png" });
  await card.getByRole("button", { name: "Hide", exact: true }).click();
  await expect(card.getByTestId("local-admin-password")).toHaveText("•".repeat(16));
  await expect(page.locator("body")).not.toContainText(first);
  // Another page forgets a revealed password.
  await card.getByRole("button", { name: "Reveal", exact: true }).click();
  await expect(card.getByTestId("local-admin-password")).toHaveText(first);
  await page.getByRole("button", { name: "Logs", exact: true }).click();
  await page.getByRole("button", { name: "Local server", exact: true }).first().click();
  await expect(page.getByTestId("local-admin-password")).toHaveText("•".repeat(16));
});

test("Copy password copies it without showing it", async ({ page }) => {
  await mockApp(page);
  const card = page.getByRole("region", { name: "Administrator credentials", exact: true });
  await card.getByRole("button", { name: "Copy password", exact: true }).click();
  await expect(card.getByRole("status")).toHaveText("Password copied to the clipboard.");
  await expect(card.getByTestId("local-admin-password")).toHaveText("•".repeat(16));
  expect(await pasted(page)).toBe(first);
});

test("Connect from this computer points at 127.0.0.1 over HTTP and never includes the password", async ({ page }) => {
  await mockApp(page);
  const panel = page.getByRole("region", { name: "Connect from this computer", exact: true });
  await expect(page.getByRole("region", { name: "Connect from another computer", exact: true })).toHaveCount(0);
  await expect(panel.getByRole("tabpanel")).toContainText("http://127.0.0.1:31337/ui/");
  await expect(panel.locator(".connection-uri code")).toHaveText("hardcharger://127.0.0.1:31337?tls=false");
  for (const [tab, text] of [["Python / ADBC", 'os.environ["HARDCHARGER_PASSWORD"]'], ["JDBC", "jdbc:hardcharger://127.0.0.1:31337?useEncryption=false"], ["CLI", "hardcharger 'hardcharger://127.0.0.1:31337?tls=false' --username 'hardcharger'"], ["JavaScript", "process.env.HARDCHARGER_PASSWORD"]] as const) {
    await panel.getByRole("tab", { name: tab, exact: true }).click();
    await expect(panel.getByRole("tabpanel")).toContainText(text);
  }
  await expect(panel).toContainText("never leave this computer");
  await page.getByRole("region", { name: "Administrator credentials", exact: true }).getByRole("button", { name: "Reveal", exact: true }).click();
  await expect(page.getByTestId("local-admin-password")).toHaveText(first);
  await expect(panel).not.toContainText(first);
  await panel.screenshot({ path: "artifacts/desktop-connect-local-python.png" });
});

test("Generate new password asks first, like Restart; Cancel changes nothing", async ({ page }) => {
  await mockApp(page);
  const card = page.getByRole("region", { name: "Administrator credentials", exact: true });
  await card.getByRole("button", { name: "Generate new password", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Generate a new administrator password?" });
  await expect(dialog).toContainText("The server restarts now");
  await expect(dialog.getByRole("button", { name: "Cancel", exact: true })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  expect(await calls(page)).not.toContain("regenerate_local_password");
  await card.getByRole("button", { name: "Generate new password", exact: true }).click();
  await dialog.getByRole("button", { name: "Generate new password", exact: true }).click();
  await expect(card.getByRole("status")).toHaveText("A new password was generated. Reveal or copy it for your clients.");
  const regenerate = await page.evaluate(() => (window as unknown as { __calls: { command: string; args: unknown }[] }).__calls.find(c => c.command === "regenerate_local_password"));
  expect(regenerate?.args).toEqual({ confirmed: true });
  await card.getByRole("button", { name: "Reveal", exact: true }).click();
  await expect(card.getByTestId("local-admin-password")).toHaveText(second);
});

test("a stopped server's dialog says the new password applies at the next start", async ({ page }) => {
  await mockApp(page, false);
  await page.getByRole("region", { name: "Administrator credentials", exact: true }).getByRole("button", { name: "Generate new password", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText("The server uses it the next time it starts.");
});

test("Export YAML leaves the local administrator password out", async ({ page }) => {
  await mockApp(page, false);
  await page.getByRole("region", { name: "Administrator credentials", exact: true }).getByRole("button", { name: "Reveal", exact: true }).click();
  await expect(page.getByTestId("local-admin-password")).toHaveText(first);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export YAML", exact: true }).click();
  const yaml = readFileSync((await (await download).path())!, "utf8");
  expect(yaml).toContain("31337");
  expect(yaml).not.toContain(first);
  expect(yaml).not.toContain("admin_password");
});
