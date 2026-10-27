import { expect, test, type Page } from "@playwright/test";

// The dashboard's side of in-app updates (gizmodata/hardcharger#7), against a mocked native
// app: the Rust side's feed selection, signatures and install are covered by its own tests.
interface Setup { running?: boolean; available?: boolean; checkError?: boolean }

async function open(page: Page, setup: Setup = {}) {
  await page.addInitScript(({ running, available, checkError }) => {
    const callbacks = new Map<number, (event: unknown) => void>();
    const listeners = new Map<string, number>();
    const calls: { command: string; args: Record<string, unknown> }[] = [];
    let next = 1;
    const release = { version: "2.2.1", notes: "### Fixed\n\n- Faster scans <script>alert(1)</script>", date: "2026-11-02" };
    const state = {
      current: "2.2.0", checking: false, checkedAt: null as number | null, available: available ? release : null,
      installing: false, error: null as string | null, preferences: { automatic: true, prereleases: false, skipped: null as string | null },
    };
    const status = { running, port: 31337, endpoint: "http://127.0.0.1:31337", version: "Hardcharger 2.2.0", error: null,
      settings: { port: 31337, start_on_open: true, server_options: {} } };
    Object.assign(window, { isTauri: true, nativeCalls: calls,
      emitNative: (event: string, payload: unknown) => callbacks.get(listeners.get(event)!)?.({ payload }),
      __TAURI_INTERNALS__: {
        transformCallback: (callback: (event: unknown) => void) => { const id = next++; callbacks.set(id, callback); return id; },
        // Replies are copies, as over real IPC: the page can't share the mock's objects.
        invoke: async (command: string, args: Record<string, unknown> & { event?: string; handler?: number }) =>
          structuredClone(await reply(command, args)),
      },
    });
    async function reply(command: string, args: Record<string, unknown> & { event?: string; handler?: number }): Promise<unknown> {
          if (command === "plugin:event|listen") { listeners.set(args.event!, args.handler!); return 1; }
          if (command.startsWith("update_")) calls.push({ command, args });
          if (command === "server_options") return [];
          if (command === "server_status") return status;
          if (command === "update_state") return state;
          if (command === "update_check") {
            if (checkError) { state.error = "Could not check for updates: error sending request"; return state; }
            state.available = release; state.checkedAt = 1798000000; return state;
          }
          if (command === "update_preferences") { state.preferences = args.preferences as typeof state.preferences; return state; }
          if (command === "update_install") { state.installing = true; return null; }
          return false;
    }
  }, { running: setup.running ?? true, available: setup.available ?? false, checkError: setup.checkError ?? false });
  await page.goto("/");
  await expect(page.getByRole("button", { name: "About Hardcharger", exact: true })).toBeVisible();
}

const calls = (page: Page) => page.evaluate(() => (window as unknown as { nativeCalls: { command: string; args: Record<string, unknown> }[] }).nativeCalls
  .filter(call => call.command !== "update_state"));

test("About shows the app version, and a manual check finds an update and shows the badge", async ({ page }) => {
  await open(page);
  await expect(page.locator("#update-badge")).toHaveCount(0);
  await page.getByRole("button", { name: "About Hardcharger", exact: true }).click();
  await expect(page.getByText("Hardcharger Desktop 2.2.0", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Check for updates", exact: true }).click();
  await expect(page.locator("#update-status")).toHaveText("Hardcharger 2.2.1 is available.");
  await expect(page.getByRole("button", { name: "Update available · 2.2.1" })).toBeVisible();
  expect(await calls(page)).toEqual([{ command: "update_check", args: {} }]);
});

test("a running server is never stopped for an update without confirmation", async ({ page }) => {
  await open(page, { available: true });
  await page.getByRole("button", { name: "Update available · 2.2.1" }).click();
  const details = page.getByRole("dialog", { name: "Hardcharger 2.2.1 is available" });
  await expect(details).toBeVisible();
  // Release notes are text: markup in them is shown, never run.
  await expect(details.getByLabel("Release notes")).toContainText("<script>alert(1)</script>");
  await expect(details.getByRole("link", { name: "Release on GitHub ↗" })).toHaveAttribute("href", "https://github.com/gizmodata/hardcharger/releases/tag/v2.2.1");
  await details.getByRole("button", { name: "Download and restart", exact: true }).click();
  const consent = page.getByRole("dialog", { name: "Restart Hardcharger to update?" });
  await expect(consent).toContainText("uncommitted transactions will be rolled back");
  await expect(consent.getByRole("button", { name: "Cancel", exact: true })).toBeFocused();
  await consent.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(details).toBeVisible();
  await details.getByRole("button", { name: "Download and restart", exact: true }).click();
  await page.keyboard.press("Escape");
  await expect(consent).not.toBeVisible();
  expect(await calls(page)).toEqual([]);
  await details.getByRole("button", { name: "Download and restart", exact: true }).click();
  // A hovered primary button keeps its accent colour (it once turned white, text and all).
  const confirm = consent.getByRole("button", { name: "Stop server and update", exact: true });
  await confirm.hover();
  const colours = await confirm.evaluate(button => {
    const probe = document.createElement("span");
    probe.style.color = "var(--accent-hover)";
    document.body.append(probe);
    const accent = getComputedStyle(probe).color;
    probe.remove();
    return { background: getComputedStyle(button).backgroundColor, text: getComputedStyle(button).color, accent };
  });
  expect(colours.background).toBe(colours.accent);
  expect(colours.text).not.toBe(colours.background);
  await confirm.click();
  await expect.poll(() => calls(page)).toEqual([{ command: "update_install", args: { confirmed: true } }]);
  // The download's progress, while the dialog can't be dismissed.
  await page.evaluate(() => (window as unknown as { emitNative: (e: string, p: unknown) => void }).emitNative("update-progress", { downloaded: 52428800, total: 104857600 }));
  await expect(details.getByRole("status")).toContainText("Downloading 50.0 MB of 100.0 MB");
  await expect(details.getByRole("progressbar")).toHaveAttribute("value", "50");
  await expect(details.getByRole("button", { name: "Later", exact: true })).toBeDisabled();
});

test("with the server stopped, an update installs without the stop confirmation", async ({ page }) => {
  await open(page, { running: false, available: true });
  await page.getByRole("button", { name: "Update available · 2.2.1" }).click();
  await page.getByRole("button", { name: "Download and install", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Restart Hardcharger to update?" })).toHaveCount(0);
  await expect.poll(() => calls(page)).toEqual([{ command: "update_install", args: { confirmed: false } }]);
});

test("skipping a version hides its badge, and the update settings are saved", async ({ page }) => {
  await open(page, { available: true });
  await page.getByRole("button", { name: "Update available · 2.2.1" }).click();
  await page.getByRole("button", { name: "Skip this version", exact: true }).click();
  await expect(page.locator("#update-badge")).toHaveCount(0);
  await page.getByRole("button", { name: "About Hardcharger", exact: true }).click();
  await page.getByLabel("Check for updates automatically").uncheck();
  await page.getByLabel("Include release candidates").check();
  expect(await calls(page)).toEqual([
    { command: "update_preferences", args: { preferences: { automatic: true, prereleases: false, skipped: "2.2.1" } } },
    { command: "update_preferences", args: { preferences: { automatic: false, prereleases: false, skipped: "2.2.1" } } },
    { command: "update_preferences", args: { preferences: { automatic: false, prereleases: true, skipped: "2.2.1" } } },
  ]);
  // About still offers the skipped version.
  await expect(page.getByRole("button", { name: "See what's new in 2.2.1" })).toBeVisible();
});

test("a failed check is reported on About and leaves the server controls alone", async ({ page }) => {
  await open(page, { checkError: true });
  await page.getByRole("button", { name: "About Hardcharger", exact: true }).click();
  await page.getByRole("button", { name: "Check for updates", exact: true }).click();
  await expect(page.locator("#update-status")).toHaveText("Couldn't check for updates: Could not check for updates: error sending request");
  await expect(page.locator("#update-badge")).toHaveCount(0);
  await page.getByRole("button", { name: /Local server/ }).click();
  await expect(page.getByRole("button", { name: "Stop", exact: true })).toBeEnabled();
});
