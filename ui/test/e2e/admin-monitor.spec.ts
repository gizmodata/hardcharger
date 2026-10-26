import { connect } from "@gizmodata/warp-sql";
import { expect, expectProblem, runDone, runSql, signIn, test } from "./fixtures.ts";
import { startOwnServer } from "./harness.ts";

test("an admin cancels executing work and the editor session remains usable", async ({ page }) => {
  expectProblem(page, {
    console:
      /ERR_INCOMPLETE_CHUNKED_ENCODING|Failed to load resource: (The network connection was lost|Connection terminated unexpectedly)/,
    required: false,
  });
  const server = await startOwnServer(["--enable-instrumentation"]);
  page.on("dialog", (dialog) => void dialog.accept());
  try {
    await signIn(page, server.admin, server.url);
    await runSql(page, "SELECT count(*) AS cancellable_monitor_work FROM range(10000000000) a, range(1000) b");
    await expect(page.getByTestId("cancel-button")).toBeVisible();
    await page.getByRole("button", { name: "SQL Monitor", exact: true }).click();
    const row = page.getByTestId("sql-monitor-table").locator("tbody tr", {
      has: page.getByRole("button", { name: /SELECT count\(\*\) AS cancellable_monitor_work/ }),
    });
    await row.getByRole("button", { name: /^Cancel work in session/ }).click();
    await expect(page.getByText(/^Cancelled work in session/)).toBeVisible();
    await expect(row).toContainText("Cancelled");
    await page.getByRole("button", { name: "Query", exact: true }).click();
    await expect(page.getByTestId("results-status")).toContainText("Cancelled after");
    await runDone(page, "SELECT 7 AS still_connected");
    await expect(page.getByRole("grid")).toContainText("still_connected");
  } finally {
    await page.goto("about:blank");
    await server.stop();
  }
});

test("an admin cancels a queued statement and ends the session holding its slot", async ({ page }) => {
  const server = await startOwnServer(["--enable-instrumentation", "--max-concurrent-statements", "1"]);
  const holder = await connect({ url: `${server.url}/warp`, ...server.admin, resultPartitions: 1 });
  const queued = await connect({ url: `${server.url}/warp`, ...server.admin, resultPartitions: 1 });
  page.on("dialog", (dialog) => void dialog.accept());
  try {
    await holder.execute("SET hardcharger.bypass_queue = false");
    await queued.execute("SET hardcharger.bypass_queue = false");
    const held = holder.query("SELECT count(*) AS monitor_slot_holder FROM range(10000000000) a, range(1000) b").then(
      () => null,
      (err: unknown) => err,
    );
    await signIn(page, server.admin, server.url);
    await page.getByRole("button", { name: "SQL Monitor", exact: true }).click();
    const table = page.getByTestId("sql-monitor-table");
    await expect(table.getByRole("button", { name: /SELECT count\(\*\) AS monitor_slot_holder/ })).toBeVisible();
    const waiting = queued.query("SELECT 42 AS monitor_queued_admin_probe").then(
      () => null,
      (err: unknown) => err,
    );
    const row = table.locator("tbody tr", {
      has: page.getByRole("button", { name: "SELECT 42 AS monitor_queued_admin_probe", exact: true }),
    });
    await expect(row.getByRole("img", { name: "Queued", exact: true })).toBeVisible();
    await row.getByRole("button", { name: /^Cancel work in session/ }).click();
    expect(await waiting).not.toBeNull();
    await expect(row).toContainText("Cancelled");
    const queuedId = queued.sessionId;
    expect((await queued.getSession()).session_id).toBe(queuedId);
    const holding = table.locator("tbody tr", {
      has: page.getByRole("button", { name: /SELECT count\(\*\) AS monitor_slot_holder/ }),
    });
    await holding.getByRole("button", { name: /^End session/ }).click();
    await expect(page.getByText(new RegExp(`^Ended session ${holder.sessionId}`))).toBeVisible();
    expect(await held).not.toBeNull();
    const answer = await queued.query("SELECT 1 AS alive");
    expect(answer.getChildAt(0)!.get(0)).toBe(1);
    expect(queued.sessionId).toBe(queuedId);
  } finally {
    await page.goto("about:blank");
    await holder.close().catch(() => {});
    await queued.close().catch(() => {});
    await server.stop();
  }
});
