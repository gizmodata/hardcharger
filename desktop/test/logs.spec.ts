import { test, expect } from "@playwright/test";

test("live tail can pause, manually refresh, and preserve an older scroll position", async ({ page }) => {
  await page.addInitScript(() => {
    let reads = 0;
    const status = { running: true, port: 31337, endpoint: "http://127.0.0.1:31337", version: "Hardcharger 2.0.0", error: null,
      settings: { port: 31337, start_on_open: true, server_options: {} } };
    Object.assign(window, { isTauri: true, __TAURI_INTERNALS__: {
      transformCallback: () => 1,
      invoke: async (command: string) => {
        if (command === "server_options") return [];
        if (command === "server_status") return status;
        if (command === "read_logs") return Array.from({ length: 200 }, (_, i) => `Earlier log line ${i}`).join("\n") + `\nLatest update ${++reads}`;
        return false;
      },
    } });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Logs", exact: true }).click();
  const output = page.getByLabel("Server log output");
  await expect(output).toContainText("Latest update 1");
  await expect(page.getByRole("checkbox", { name: "Live tail", exact: true })).toBeChecked();
  await output.evaluate(el => { el.scrollTop = 0; });
  await expect(output).toContainText("Latest update 2", { timeout: 5000 });
  expect(await output.evaluate(el => el.scrollTop)).toBe(0);
  await page.getByRole("checkbox", { name: "Live tail", exact: true }).uncheck();
  const paused = await output.textContent();
  await page.waitForTimeout(2300); // longer than a polling interval: paused means no update
  await expect(output).toHaveText(paused!);
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(output).not.toHaveText(paused!);
  await expect(page.getByRole("status")).toContainText("Paused");
  await page.screenshot({ path: "artifacts/desktop-logs-live-tail.png" });
});
