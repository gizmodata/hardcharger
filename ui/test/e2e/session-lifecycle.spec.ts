import { connect } from "@gizmodata/chargewire";

import { activeTab, expect, runDone, signIn, test } from "./fixtures.ts";
import { startOwnServer } from "./harness.ts";

test("administrator navigation and closed query inspectors return to the baseline session count", async ({ page }) => {
  const server = await startOwnServer(["--enable-instrumentation"]);
  const admin = await connect({ url: `${server.url}/chargewire`, ...server.admin });
  const ids = async () =>
    [...(await admin.query("SELECT session_id FROM hardcharger_sessions()"))]
      .map((row) => String(row.toJSON().session_id))
      .sort();
  try {
    await signIn(page, server.admin, server.url);
    await runDone(page, "SELECT 42 AS lifecycle_probe");
    const baseline = await ids();
    for (let cycle = 0; cycle < 3; cycle++) {
      await page.getByRole("button", { name: "SQL Monitor", exact: true }).click();
      await expect(page.getByTestId("sql-monitor-table")).toContainText("lifecycle_probe");
      await page.getByRole("button", { name: "Server Configuration", exact: true }).click();
      await expect(page.getByTestId("server-configuration-table")).toBeVisible();
      await page.getByRole("button", { name: "Sessions", exact: true }).click();
      await expect(page.getByTestId("sessions-table")).toBeVisible();
      await expect.poll(ids).toEqual(baseline);
    }
    await page.getByRole("button", { name: "SQL Monitor", exact: true }).click();
    const monitor = page.getByTestId("sql-monitor-table");
    await expect(monitor).toContainText("lifecycle_probe");
    const monitorBaseline = await ids();
    for (let cycle = 0; cycle < 3; cycle++) {
      const opened = page.waitForEvent("popup");
      await monitor.getByRole("button", { name: /SELECT 42 AS lifecycle_probe/ }).click();
      const details = await opened;
      await expect(details.getByRole("region", { name: "Captured query profile" })).toBeVisible();
      // Model a native webview that is destroyed by window.close without unload events.
      // Hold session DELETEs: the native close must not be requested until they finish.
      await details.evaluate(() => {
        Object.assign(window, { nativeCloseCalled: false });
        window.close = () => {
          (window as unknown as { nativeCloseCalled: boolean }).nativeCloseCalled = true;
        };
      });
      let release!: () => void;
      const gate = new Promise<void>((resolve) => {
        release = resolve;
      });
      let deletes = 0;
      await details.route(/\/chargewire\/v1\/sessions\/[^/?]+$/, async (route) => {
        if (route.request().method() === "DELETE") {
          deletes++;
          await gate;
        }
        await route.continue();
      });
      if (cycle % 2 === 0) await details.getByRole("button", { name: "Close window", exact: true }).click();
      else await details.evaluate(() => window.dispatchEvent(new Event("hardcharger:close-window")));
      await expect.poll(() => deletes).toBeGreaterThan(0);
      expect(
        await details.evaluate(() => (window as unknown as { nativeCloseCalled: boolean }).nativeCloseCalled),
      ).toBe(false);
      release();
      await expect
        .poll(() => details.evaluate(() => (window as unknown as { nativeCloseCalled: boolean }).nativeCloseCalled))
        .toBe(true);
      await expect.poll(ids).toEqual(monitorBaseline);
      await details.close();
    }
  } finally {
    await page.goto("about:blank");
    await admin.close();
    await server.stop();
  }
});

test("native workspace close ends all tab sessions before destroying the view, across reopens", async ({ context }) => {
  const server = await startOwnServer([]);
  const admin = await connect({ url: `${server.url}/chargewire`, ...server.admin });
  const ids = async () =>
    [...(await admin.query("SELECT session_id FROM hardcharger_sessions()"))]
      .map((row) => String(row.toJSON().session_id))
      .sort();
  try {
    const baseline = await ids();
    for (let cycle = 0; cycle < 3; cycle++) {
      const workspace = await context.newPage();
      try {
        await signIn(workspace, server.admin, server.url);
        await runDone(workspace, "SELECT 42 AS workspace_close_probe");
        await workspace.getByRole("button", { name: "New tab", exact: true }).click();
        await runDone(workspace, "SELECT 43 AS second_session_probe");
        expect((await ids()).length).toBeGreaterThanOrEqual(baseline.length + 3);
        await workspace.evaluate(() => {
          Object.assign(window, { nativeCloseCalled: false });
          window.close = () => {
            Object.assign(window, { nativeCloseCalled: true });
          };
        });
        let release!: () => void;
        const gate = new Promise<void>((resolve) => {
          release = resolve;
        });
        let deletes = 0;
        await workspace.route(/\/chargewire\/v1\/sessions\/[^/?]+$/, async (route) => {
          if (route.request().method() === "DELETE") {
            deletes++;
            await gate;
          }
          await route.continue();
        });
        await workspace.evaluate(() => window.dispatchEvent(new Event("hardcharger:close-window")));
        await expect.poll(() => deletes).toBeGreaterThanOrEqual(3);
        expect(
          await workspace.evaluate(() => (window as unknown as { nativeCloseCalled: boolean }).nativeCloseCalled),
        ).toBe(false);
        release();
        await expect
          .poll(() => workspace.evaluate(() => (window as unknown as { nativeCloseCalled: boolean }).nativeCloseCalled))
          .toBe(true);
        await expect.poll(ids).toEqual(baseline);
      } finally {
        await workspace.close();
      }
    }
  } finally {
    await admin.close();
    await server.stop();
  }
});

test("native workspace close asks before rolling back a transaction and cancellation keeps it usable", async ({
  page,
}) => {
  const server = await startOwnServer([]);
  const admin = await connect({ url: `${server.url}/chargewire`, ...server.admin });
  const ids = async () =>
    [...(await admin.query("SELECT session_id FROM hardcharger_sessions()"))]
      .map((row) => String(row.toJSON().session_id))
      .sort();
  try {
    const baseline = await ids();
    await signIn(page, server.admin, server.url);
    await runDone(page, "BEGIN");
    await expect(activeTab(page).getByTestId("in-transaction")).toBeVisible();
    const opened = await ids();
    await page.evaluate(() => {
      Object.assign(window, { nativeCloseCalled: false });
      window.close = () => {
        Object.assign(window, { nativeCloseCalled: true });
      };
      window.dispatchEvent(new Event("hardcharger:close-window"));
    });
    const dialog = page.getByRole("dialog", { name: "Close SQL workspace?", exact: true });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Cancel", exact: true })).toBeFocused();
    await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(dialog).toHaveCount(0);
    expect(await ids()).toEqual(opened);
    await runDone(page, "SELECT 44 AS still_in_transaction");
    await expect(activeTab(page).getByTestId("in-transaction")).toBeVisible();
    await page.evaluate(() => window.dispatchEvent(new Event("hardcharger:close-window")));
    await expect(dialog).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    expect(await ids()).toEqual(opened);
    expect(await page.evaluate(() => (window as unknown as { nativeCloseCalled: boolean }).nativeCloseCalled)).toBe(
      false,
    );
    await page.evaluate(() => window.dispatchEvent(new Event("hardcharger:close-window")));
    await dialog.getByRole("button", { name: "Close workspace", exact: true }).click();
    await expect
      .poll(() => page.evaluate(() => (window as unknown as { nativeCloseCalled: boolean }).nativeCloseCalled))
      .toBe(true);
    await expect.poll(ids).toEqual(baseline);
  } finally {
    await page.goto("about:blank");
    await admin.close();
    await server.stop();
  }
});
