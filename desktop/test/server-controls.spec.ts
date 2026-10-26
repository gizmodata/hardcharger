import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const callbacks = new Map<number, (event: unknown) => void>();
    const listeners = new Map<string, number>();
    const calls: { action: string; confirmed: boolean }[] = [];
    let next = 1;
    const status = { running: true, port: 31337, endpoint: "http://127.0.0.1:31337", version: "Hardcharger 2.0.0", error: null,
      settings: { port: 31337, start_on_open: true, server_options: {} } };
    Object.assign(window, { isTauri: true, controlCalls: calls,
      requestControl: (action: string) => callbacks.get(listeners.get("server-control-requested")!)?.({ payload: action }),
      __TAURI_INTERNALS__: {
        transformCallback: (callback: (event: unknown) => void) => { const id = next++; callbacks.set(id, callback); return id; },
        invoke: async (command: string, args: { event?: string; handler?: number; action: string; confirmed: boolean }) => {
          if (command === "plugin:event|listen") { listeners.set(args.event!, args.handler!); return 1; }
          if (command === "server_options") return [];
          if (command === "server_status") return status;
          if (command === "server_control") { calls.push({ action: args.action, confirmed: args.confirmed }); status.running = args.action !== "stop"; return status; }
          return false;
        },
      },
    });
  });
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Stop", exact: true })).toBeEnabled();
});

for (const action of ["Stop", "Restart"]) {
  test(`${action} waits for confirmation and Cancel or Escape leaves the server untouched`, async ({ page }) => {
    const calls = () => page.evaluate(() => (window as unknown as { controlCalls: unknown[] }).controlCalls);
    const dialog = page.getByRole("dialog", { name: `${action} Hardcharger server?` });
    await page.getByRole("button", { name: action, exact: true }).click();
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText("uncommitted transactions will be rolled back");
    await expect(dialog.getByRole("button", { name: "Cancel", exact: true })).toBeFocused();
    expect(await calls()).toEqual([]);
    await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(dialog).not.toBeVisible();
    expect(await calls()).toEqual([]);
    await page.getByRole("button", { name: action, exact: true }).click();
    await page.keyboard.press("Escape");
    await expect(dialog).not.toBeVisible();
    expect(await calls()).toEqual([]);
    await page.getByRole("button", { name: action, exact: true }).click();
    await page.screenshot({ path: `artifacts/desktop-confirm-${action.toLowerCase()}.png` });
    await dialog.getByRole("button", { name: `${action} server`, exact: true }).click();
    await expect.poll(calls).toEqual([{ action: action.toLowerCase(), confirmed: true }]);
    await expect(dialog).not.toBeVisible();
  });

  test(`native menu or tray ${action} uses the same confirmation`, async ({ page }) => {
    await page.getByRole("button", { name: "About Hardcharger", exact: true }).click();
    await page.evaluate(value => (window as unknown as { requestControl: (action: string) => void }).requestControl(value), action.toLowerCase());
    const dialog = page.getByRole("dialog", { name: `${action} Hardcharger server?` });
    await expect(dialog).toBeVisible();
    expect(await page.evaluate(() => (window as unknown as { controlCalls: unknown[] }).controlCalls)).toEqual([]);
    await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(dialog).not.toBeVisible();
  });
}
