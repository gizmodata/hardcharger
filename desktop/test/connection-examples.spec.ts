import { expect, test } from "@playwright/test";
import { connectionExamples } from "../src/connectionExamples";

test("client URIs preserve IPv6/ports and only disable TLS for an HTTP server", () => {
  const secure = connectionExamples("https://[2001:db8::12]:443", "reader");
  expect(secure.uri).toBe("hardcharger://[2001:db8::12]:443");
  for (const id of ["python", "cli", "javascript"]) expect(secure.examples.find(e => e.id === id)!.code).toContain(secure.uri);
  expect(secure.examples.find(e => e.id === "jdbc")!.code).toContain("jdbc:hardcharger://[2001:db8::12]:443");
  expect(secure.examples.find(e => e.id === "python")!.code).toContain("tls_root_certs=");
  const plain = connectionExamples("http://database.example.com:31337", "reader");
  expect(plain.uri).toBe("hardcharger://database.example.com:31337?tls=false");
  expect(plain.examples.find(e => e.id === "jdbc")!.code).toContain("?useEncryption=false");
  expect(plain.examples.find(e => e.id === "python")!.code).not.toContain("tls_root_certs=");
  expect(() => connectionExamples("https://user:secret@example.com", "reader")).toThrow();
});

test("network examples are accessible, copy real text and never include a saved secret", async ({ page }) => {
  await page.addInitScript(() => {
    const status = { running: true, port: 31337, endpoint: "https://database.example.com:31337", version: "Hardcharger 2.1.0", error: null,
      settings: { port: 31337, start_on_open: true, server_options: {}, network: { enabled: true, tls: true, username: "reader", password: "must-not-be-shared", server_name: "database.example.com" } } };
    Object.assign(window, { isTauri: true, __TAURI_INTERNALS__: { transformCallback: () => 1, invoke: async (command: string) => command === "server_status" ? status : command === "server_options" ? [] : false } });
  });
  await page.goto("/");
  const panel = page.getByRole("region", { name: "Connect from another computer", exact: true });
  await expect(panel.getByRole("tab", { name: "Web UI", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(panel.getByRole("tabpanel")).toContainText("https://database.example.com:31337/ui/");
  await panel.getByRole("tab", { name: "Python / ADBC", exact: true }).click();
  await expect(panel.getByRole("tabpanel")).toContainText("pip install adbc-driver-hardcharger");
  await expect(panel.getByRole("tabpanel")).toContainText("hardcharger://database.example.com:31337");
  await expect(panel).not.toContainText("must-not-be-shared");
  await panel.getByRole("button", { name: "Copy example", exact: true }).click();
  await expect(panel.getByRole("status")).toHaveText("Copied to clipboard.");
  // Read the clipboard by pasting, as the web UI's Safari tests do.
  const paste = page.locator("#clipboard-proof");
  await page.evaluate(() => { const input = document.createElement("textarea"); input.id = "clipboard-proof"; document.body.append(input); });
  await paste.focus();
  await page.keyboard.press("ControlOrMeta+V");
  expect(await paste.inputValue()).toContain("from adbc_driver_hardcharger import dbapi as hardcharger");
  expect(await paste.inputValue()).not.toContain("must-not-be-shared");
  await paste.evaluate(el => el.remove());
  await panel.getByRole("tab", { name: "Python / ADBC", exact: true }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(panel.getByRole("tab", { name: "JDBC", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(panel.getByRole("tabpanel")).toContainText("jdbc:hardcharger://database.example.com:31337");
  await panel.getByRole("tab", { name: "JavaScript", exact: true }).click();
  await expect(panel.getByRole("tabpanel")).toContainText("@gizmodata/hardcharger");
  await panel.screenshot({ path: "artifacts/desktop-connect-javascript-light.png" });
  await page.getByRole("button", { name: "Change color theme" }).click();
  await panel.screenshot({ path: "artifacts/desktop-connect-javascript-dark.png" });
});
