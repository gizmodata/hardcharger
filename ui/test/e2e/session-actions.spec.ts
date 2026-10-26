import { connect } from "@gizmodata/warp-sql";

import { expect, signIn, test } from "./fixtures.ts";
import { startOwnServer } from "./harness.ts";

test("Sessions shows client IP and confirms ending a session without browser confirm", async ({ page, harness }) => {
  const victim = await connect({ url: `${harness.serverUrl}/warp`, ...harness.admin });
  try {
    await page.addInitScript(() => {
      window.confirm = () => false;
    });
    await signIn(page, harness.admin);
    await page.getByRole("button", { name: "Sessions", exact: true }).click();
    const table = page.getByTestId("sessions-table");
    await expect(table.getByRole("columnheader", { name: "Client IP", exact: true })).toBeVisible();
    const row = table.locator("tbody tr").filter({ hasText: victim.sessionId });
    await expect(row).toContainText("This computer");
    await expect(row).toContainText("127.0.0.1");
    await row.getByRole("button", { name: `End session ${victim.sessionId}`, exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "End session?", exact: true });
    await expect(dialog).toContainText(victim.sessionId);
    await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
    expect((await victim.query("SELECT 42 AS answer")).getChildAt(0)?.get(0)).toBe(42);
    await row.getByRole("button", { name: `End session ${victim.sessionId}`, exact: true }).click();
    await dialog.getByRole("button", { name: "End session", exact: true }).click();
    await expect(row).toHaveCount(0);
    await expect(victim.query("SELECT 1")).rejects.toThrow();
  } finally {
    await victim.close().catch(() => {});
  }
});

test("SQL Monitor End session disconnects its target after an in-page confirmation", async ({ page }) => {
  const server = await startOwnServer(["--enable-instrumentation"]);
  const victim = await connect({ url: `${server.url}/warp`, ...server.admin });
  try {
    await victim.query("SELECT 1 AS end_session_monitor_probe");
    await page.addInitScript(() => {
      window.confirm = () => false;
    });
    await signIn(page, server.admin, server.url);
    await page.getByRole("button", { name: "SQL Monitor", exact: true }).click();
    await page.getByLabel("Filter monitored queries").fill("end_session_monitor_probe");
    const end = page
      .getByTestId("sql-monitor-table")
      .getByRole("button", { name: `End session ${victim.sessionId}`, exact: true });
    await expect(end).toBeEnabled();
    await end.click();
    const dialog = page.getByRole("dialog", { name: "End session?", exact: true });
    await expect(dialog).toContainText("queued and in-flight work");
    await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
    expect((await victim.query("SELECT 42 AS answer")).getChildAt(0)?.get(0)).toBe(42);
    await end.click();
    await dialog.getByRole("button", { name: "End session", exact: true }).click();
    await expect(page.getByRole("status").filter({ hasText: `Ended session ${victim.sessionId}.` })).toBeVisible();
    await expect(end).toBeDisabled();
    await expect(victim.query("SELECT 1")).rejects.toThrow();
  } finally {
    await page.goto("about:blank");
    await victim.close().catch(() => {});
    await server.stop();
  }
});

test("SQL Monitor Cancel work confirms cancellation and keeps the session usable", async ({ page }) => {
  const server = await startOwnServer(["--enable-instrumentation"]);
  const victim = await connect({ url: `${server.url}/warp`, ...server.admin });
  let running: Promise<unknown> | undefined;
  try {
    running = victim
      .query("SELECT sum(a.range*b.range) AS cancel_work_monitor_probe FROM range(100000000) a, range(100000) b")
      .catch((error: unknown) => error);
    await page.addInitScript(() => {
      window.confirm = () => false;
    });
    await signIn(page, server.admin, server.url);
    await page.getByRole("button", { name: "SQL Monitor", exact: true }).click();
    await page.getByLabel("Filter monitored queries").fill("cancel_work_monitor_probe");
    await page
      .getByTestId("sql-monitor-table")
      .getByRole("button", { name: `Cancel work in session ${victim.sessionId}`, exact: true })
      .click();
    const dialog = page.getByRole("dialog", { name: "Cancel session work?", exact: true });
    await expect(dialog).toContainText("session stays connected");
    await dialog.getByRole("button", { name: "Cancel work", exact: true }).click();
    await running;
    expect((await victim.query("SELECT 42 AS answer")).getChildAt(0)?.get(0)).toBe(42);
  } finally {
    await page.goto("about:blank");
    await victim.close().catch(() => {});
    await running;
    await server.stop();
  }
});
