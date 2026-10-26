import { connect } from "@gizmodata/warp-sql";

import { expect, runDone, signIn, test } from "./fixtures.ts";
import { startOwnServer } from "./harness.ts";

test("administrator navigation and closed query inspectors return to the baseline session count", async ({ page }) => {
  const server = await startOwnServer(["--enable-instrumentation"]);
  const admin = await connect({ url: `${server.url}/warp`, ...server.admin });
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
      await details.route(/\/warp\/v1\/sessions\/[^/?]+$/, async (route) => {
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
