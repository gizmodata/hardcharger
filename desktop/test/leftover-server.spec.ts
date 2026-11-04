import { expect, test } from "@playwright/test";

// A server the app started in an earlier run and that outlived it (issue #28) is offered for
// stopping on the Local server page, instead of failing Start with "port already in use".
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const callbacks = new Map<number, (event: unknown) => void>();
    const listeners = new Map<string, number>();
    const calls: string[] = [];
    let next = 1;
    const status: Record<string, unknown> = {
      running: false, pid: null, port: 31337, endpoint: "http://127.0.0.1:31337", version: "Hardcharger 2.7.0", error: null,
      settings: { port: 31337, start_on_open: true, server_options: {} }, leftover: { pid: 4242, host: "127.0.0.1", port: 31337 },
    };
    Object.assign(window, { isTauri: true, controlCalls: calls,
      __TAURI_INTERNALS__: {
        transformCallback: (callback: (event: unknown) => void) => { const id = next++; callbacks.set(id, callback); return id; },
        invoke: async (command: string, args: { event?: string; handler?: number; action?: string }) => {
          if (command === "plugin:event|listen") { listeners.set(args.event!, args.handler!); return 1; }
          if (command === "server_options") return [];
          if (command === "server_status") return status;
          if (command === "stop_leftover") { calls.push(command); status.leftover = null; return status; }
          if (command === "server_control") { calls.push(`${command}:${args.action}`); status.running = args.action !== "stop"; return status; }
          return false;
        },
      },
    });
  });
  await page.goto("/");
});

test("a server left behind by a previous run is named, and Stop it stops it", async ({ page }) => {
  const banner = page.locator(".leftover-server");
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("A server from a previous run is still running");
  await expect(banner).toContainText("PID 4242");
  await expect(banner).toContainText("port 31337");
  await page.screenshot({ path: "artifacts/desktop-leftover-server.png" });
  await banner.getByRole("button", { name: "Stop it", exact: true }).click();
  await expect.poll(() => page.evaluate(() => (window as unknown as { controlCalls: string[] }).controlCalls)).toEqual(["stop_leftover"]);
  await expect(banner).toBeHidden();
  await expect(page.getByRole("button", { name: "Start server", exact: true })).toBeEnabled();
});

test("the banner is not shown while this run's server is running", async ({ page }) => {
  await page.getByRole("button", { name: "Start server", exact: true }).click();
  await expect(page.getByRole("button", { name: "Stop", exact: true })).toBeEnabled();
  await expect(page.locator(".leftover-server")).toBeHidden();
});
