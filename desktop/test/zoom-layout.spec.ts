// #55: the controller at 150% and 200% text size in its smallest window (860×640). A WebView's
// page zoom lays the page out in a viewport of the window's size divided by the zoom, with that
// many device pixels per CSS pixel, which is what these tests emulate. Nothing may need a
// horizontal scroll, and what must stay reachable (Save settings, the dialogs' buttons) stays
// on screen.
import { expect, test, type Page } from "@playwright/test";

const window = { width: 860, height: 640 };

async function mockApp(page: Page) {
  await page.addInitScript(() => {
    const status = () => ({ running: true, pid: 4242, port: 31337, endpoint: "http://127.0.0.1:31337", version: "Hardcharger 2.14.0", error: null,
      database: "/Users/someone/Library/Application Support/com.gizmodata.hardcharger.desktop/Hardcharger.duckdb", logFile: "server.log", uptimeSeconds: 3725, databaseBytes: 122683392,
      settings: { port: 31337, start_on_open: true, server_options: { memory_limit: "1GB" }, network: { enabled: false, tls: true, username: "hardcharger", password: "", server_name: "", listen_address: "0.0.0.0", tls_cert: "", tls_key: "" }, remote_servers: [] },
      localAdmin: { username: "hardcharger", error: null } });
    Object.assign(window, {
      isTauri: true,
      __TAURI_INTERNALS__: {
        transformCallback: () => 1,
        invoke: async (command: string) => {
          if (command === "server_status") return status();
          if (command === "local_admin_password") return "Abcde-23456-fghij-KLMNP";
          if (command === "zoom_state") return { level: 1, levels: [0.5, 0.75, 0.85, 1, 1.15, 1.25, 1.5, 1.75, 2, 2.5, 3] };
          if (command === "read_logs") return Array.from({ length: 200 }, (_, i) => `2026-11-18T12:00:${String(i % 60).padStart(2, "0")}Z INFO a long server log line number ${i} with enough text to wrap at a small width`).join("\n");
          if (command === "server_options") return [{ name: "memory_limit", type: "string", group: "Resources", description: "The most memory DuckDB may use", default: "" }];
          return false;
        },
      },
    });
  });
}

async function noHorizontalScroll(page: Page, what: string) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
  expect(overflow, `${what} scrolls sideways`).toBeLessThanOrEqual(0);
}

for (const zoom of [1.5, 2]) {
  test(`the controller at ${zoom * 100}% in its smallest window`, async ({ browser }) => {
    const context = await browser.newContext({
      viewport: { width: Math.floor(window.width / zoom), height: Math.floor(window.height / zoom) },
      deviceScaleFactor: zoom,
    });
    const page = await context.newPage();
    await mockApp(page);
    await page.goto("/");
    const shot = (name: string) => page.screenshot({ path: `artifacts/zoom-${zoom * 100}-${name}.png` });
    await expect(page.getByRole("heading", { name: "Local server", exact: true })).toBeVisible();
    await noHorizontalScroll(page, "Local server");
    await shot("dashboard");
    await page.getByRole("region", { name: "Administrator credentials", exact: true }).getByRole("button", { name: "Reveal", exact: true }).click();
    await page.getByRole("region", { name: "Administrator credentials", exact: true }).scrollIntoViewIfNeeded();
    await noHorizontalScroll(page, "the revealed password");
    await shot("credentials");
    // Stop's confirmation fits, with both buttons on screen.
    await page.getByRole("button", { name: "Stop", exact: true }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Cancel", exact: true })).toBeInViewport();
    await dialog.getByRole("button", { name: "Stop server", exact: true }).scrollIntoViewIfNeeded();
    await expect(dialog.getByRole("button", { name: "Stop server", exact: true })).toBeInViewport();
    await shot("stop-dialog");
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await expect(page.getByRole("spinbutton", { name: "Port" })).toBeVisible();
    await noHorizontalScroll(page, "Settings");
    // The Save bar stays on screen while the form scrolls.
    await page.getByRole("combobox", { name: "Text size" }).scrollIntoViewIfNeeded();
    await expect(page.getByRole("button", { name: "Save settings", exact: true })).toBeInViewport();
    await shot("settings");
    await page.getByRole("button", { name: "Logs", exact: true }).click();
    await expect(page.getByRole("region", { name: "Server log output" }).or(page.locator("#log-output"))).toBeVisible();
    await noHorizontalScroll(page, "Logs");
    await shot("logs");
    await page.getByRole("button", { name: "About Hardcharger" }).click();
    await noHorizontalScroll(page, "About");
    await shot("about");
    await context.close();
  });
}
