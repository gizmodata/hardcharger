import { connect } from "@gizmodata/chargewire";

import { activeTab, editorFocused, expect, pasted, runDone, signIn, test, watchPage } from "./fixtures.ts";
import { startOwnServer } from "./harness.ts";

test("query windows retain profiles and refresh independently of the monitor", async ({ page }, testInfo) => {
  const server = await startOwnServer(["--enable-instrumentation"]);
  const connection = await connect({ url: `${server.url}/chargewire`, ...server.admin, resultPartitions: 1 });
  const sql =
    "SELECT sum(i) AS popout_profile_probe FROM range(100000) t(i)\n" +
    Array.from({ length: 100 }, (_, i) => `-- Clipboard scroll line ${i}`).join("\n");
  try {
    await connection.execute("SET hardcharger.capture_query_profile = 'detailed'");
    await connection.query(sql);
    await signIn(page, server.admin, server.url);
    await page.getByRole("button", { name: "SQL Monitor", exact: true }).click();
    await expect(page.getByLabel("Query details", { exact: true })).toHaveValue("window");
    const opened = page.waitForEvent("popup");
    await page
      .getByTestId("sql-monitor-table")
      .getByRole("button", { name: /popout_profile_probe/ })
      .click();
    const details = await opened;
    const check = await watchPage(details);
    await expect(details.getByRole("heading", { name: "Query details", exact: true })).toBeVisible();
    await expect(details.getByRole("region", { name: "Statement details" })).toContainText(sql);
    await expect(details.getByRole("table", { name: "Query step timings", exact: true })).toBeVisible();
    await expect(details.getByTestId("query-timing-total")).not.toContainText("so far");
    const sqlBlock = details.getByLabel("SQL text", { exact: true });
    expect(await sqlBlock.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true);
    await sqlBlock.focus();
    await details.keyboard.press("PageDown");
    await expect.poll(() => sqlBlock.evaluate((el) => el.scrollTop)).toBeGreaterThan(0);
    await details.getByRole("button", { name: "Copy SQL", exact: true }).click();
    await expect(details.getByRole("status").filter({ hasText: "Copied to clipboard." })).toBeVisible();
    expect(await pasted(details)).toBe(sql);
    await details.getByRole("button", { name: "Formatted", exact: true }).click();
    const formatted = await sqlBlock.textContent();
    expect(formatted).not.toBe(sql);
    await details.getByRole("button", { name: "Copy SQL", exact: true }).click();
    expect(await pasted(details)).toBe(formatted);
    await details.getByRole("button", { name: "Original", exact: true }).click();
    await expect(sqlBlock).toHaveText(sql);

    await expect(details.getByTestId("query-profile-plan")).toContainText("UNGROUPED_AGGREGATE");
    await expect(page.getByRole("region", { name: "Statement details" })).toHaveCount(0);
    await page.getByLabel("Filter monitored queries").fill("no_match_popout");
    await details.reload();
    await expect(details.getByTestId("query-profile-plan")).toContainText("UNGROUPED_AGGREGATE");
    // The detail page has its own sessions and short-lived sign-in; closing the
    // original workspace cannot close them or require another password.
    await page.goto("about:blank");
    await details.reload();
    await expect(details.getByRole("region", { name: "Statement details" })).toContainText("Succeeded");
    await expect(details.getByTestId("query-profile-plan")).toBeVisible();
    await details.screenshot({
      path: `../desktop/artifacts/sql-monitor/query-window-${testInfo.project.name}.png`,
      fullPage: true,
    });
    await details.getByRole("button", { name: "Toggle color theme", exact: true }).click();
    await details.screenshot({
      path: `../desktop/artifacts/sql-monitor/query-window-dark-${testInfo.project.name}.png`,
      fullPage: true,
    });
    const count = await connection.query(
      `SELECT count(*) FROM _hardcharger_instr.execution_details WHERE sql_text = '${sql}'`,
    );
    expect(count.getChildAt(0)!.get(0)).toBe(1n);
    const closed = details.waitForEvent("close");
    await details.getByRole("button", { name: "Close window", exact: true }).click();
    await closed;
    check();
  } finally {
    await connection.close();
    await page.goto("about:blank");
    await server.stop();
  }
});

test("blocked windows fall back to inline details and the layout preference is remembered", async ({ page }) => {
  const server = await startOwnServer(["--enable-instrumentation"]);
  const connection = await connect({ url: `${server.url}/chargewire`, ...server.admin });
  try {
    await connection.query("SELECT 42 AS blocked_query_window_probe");
    await signIn(page, server.admin, server.url);
    await page.getByRole("button", { name: "SQL Monitor", exact: true }).click();
    await page.evaluate(() => {
      window.open = () => null;
    });
    await page
      .getByTestId("sql-monitor-table")
      .getByRole("button", { name: /blocked_query_window_probe/ })
      .click();
    await expect(page.getByRole("region", { name: "Statement details" })).toContainText("blocked_query_window_probe");
    await expect(page.getByRole("region", { name: "Captured query profile" })).toContainText("No profile was captured");
    await expect(page.getByRole("status").filter({ hasText: "window was blocked" })).toBeVisible();
    await page.getByLabel("Query details", { exact: true }).selectOption("inline");
    await page.reload();
    await expect(page.getByLabel("Query details", { exact: true })).toHaveValue("inline");
  } finally {
    await connection.close();
    await page.goto("about:blank");
    await server.stop();
  }
});

test("a query window follows an executing statement through cancellation", async ({ page }, testInfo) => {
  const server = await startOwnServer(["--enable-instrumentation"]);
  const target = await connect({ url: `${server.url}/chargewire`, ...server.admin });
  const admin = await connect({ url: `${server.url}/chargewire`, ...server.admin });
  let running: Promise<unknown> | undefined;
  try {
    running = target
      .query("SELECT count(*) AS live_window_probe FROM range(10000000000) a, range(1000) b")
      .catch(() => undefined);
    await signIn(page, server.admin, server.url);
    await page.getByRole("button", { name: "SQL Monitor", exact: true }).click();
    const opened = page.waitForEvent("popup");
    await page
      .getByTestId("sql-monitor-table")
      .getByRole("button", { name: /live_window_probe/ })
      .click();
    const details = await opened;
    const check = await watchPage(details);
    await expect(
      details
        .getByRole("region", { name: "Statement details" })
        .getByRole("heading", { name: "Executing", exact: true }),
    ).toBeVisible();
    const executionTiming = details.getByTestId("query-timing-execution");
    const totalTiming = details.getByTestId("query-timing-total");
    await expect(totalTiming).toContainText("so far");
    const initialElapsed = await totalTiming.locator("td").last().innerText();
    await expect.poll(() => totalTiming.locator("td").last().innerText()).not.toBe(initialElapsed);
    await admin.query(`SELECT hardcharger_cancel('${target.sessionId}')`);
    await running;
    await expect(
      details
        .getByRole("region", { name: "Statement details" })
        .getByRole("heading", { name: "Cancelled", exact: true }),
    ).toBeVisible();
    await expect(executionTiming).not.toContainText("so far");
    await expect(details.getByTestId("query-timing-total")).not.toContainText("so far");
    await details.setViewportSize({ width: 1100, height: 1100 });
    await details.screenshot({
      path: `../desktop/artifacts/sql-monitor/query-timings-light-${testInfo.project.name}.png`,
    });
    await details.getByRole("button", { name: "Toggle color theme", exact: true }).click();
    await details.screenshot({
      path: `../desktop/artifacts/sql-monitor/query-timings-dark-${testInfo.project.name}.png`,
    });
    await details.close();
    check();
  } finally {
    await target.close();
    await running;
    await admin.close();
    await page.goto("about:blank");
    await server.stop();
  }
});

test("Query results open the exact execution for each result set and update without rerunning SQL", async ({
  page,
}, testInfo) => {
  const server = await startOwnServer(["--enable-instrumentation"]);
  const conn = await connect({ url: `${server.url}/chargewire`, ...server.admin });
  try {
    await signIn(page, server.admin, server.url);
    await runDone(page, "SET hardcharger.capture_query_profile = 'detailed'");
    await runDone(
      page,
      "SELECT sum(i) AS result_details_first FROM range(10) t(i); SELECT 42 AS result_details_second",
      "ControlOrMeta+Shift+Enter",
    );
    const secondLink = activeTab(page).getByRole("link", { name: "SQL details" });
    const secondHref = await secondLink.getAttribute("href");
    const opened = page.waitForEvent("popup");
    await secondLink.click();
    const details = await opened;
    const check = await watchPage(details);
    await expect(details.getByRole("region", { name: "Statement details" })).toContainText("result_details_second");
    await expect(details.getByTestId("query-profile-plan")).toBeVisible();
    await page.getByRole("tab", { name: /^Result 1/ }).click();
    const firstLink = activeTab(page).getByRole("link", { name: "SQL details" });
    expect(await firstLink.getAttribute("href")).not.toBe(secondHref);
    const openedFirst = page.waitForEvent("popup");
    await firstLink.click();
    const firstDetails = await openedFirst;
    const checkFirst = await watchPage(firstDetails);
    await expect(firstDetails.getByRole("region", { name: "Statement details" })).toContainText("result_details_first");
    await expect(firstDetails.getByTestId("query-profile-plan")).toContainText("UNGROUPED_AGGREGATE");
    await page.screenshot({ path: testInfo.outputPath("result-sql-details.png"), fullPage: true });
    await page.getByRole("tab", { name: "Messages", exact: true }).click();
    await expect(activeTab(page).getByTestId("messages").getByRole("link", { name: "SQL details" })).toHaveCount(2);
    await runDone(page, "CREATE TABLE result_details_update AS SELECT 1 AS value");
    await expect(activeTab(page).getByRole("link", { name: "SQL details" })).toBeVisible();
    await details.reload();
    await expect(details.getByRole("region", { name: "Statement details" })).toContainText("result_details_second");
    const counts = await conn.query(
      "SELECT sql_text, count(*) AS n FROM _hardcharger_instr.execution_details WHERE sql_text LIKE 'SELECT%AS result_details_%' GROUP BY sql_text",
    );
    expect([...counts.getChild("n")!]).toEqual([1n, 1n]);
    await details.close();
    await firstDetails.close();
    check();
    checkFirst();
  } finally {
    await conn.close();
    await page.goto("about:blank");
    await server.stop();
  }
});

test("Query result details and profiles follow the remote server even when local instrumentation is off", async ({
  page,
  harness,
}) => {
  const remote = await startOwnServer([
    "--enable-instrumentation",
    "--init-sql-commands",
    "CREATE TABLE remote_details AS SELECT 1 AS id",
  ]);
  try {
    await signIn(page, harness.admin);
    await editorFocused(page);
    await page.getByTestId("explorer-add-server").click();
    const dialog = page.getByTestId("add-server-dialog");
    const url = new URL(remote.url);
    await dialog.getByLabel("Display name", { exact: true }).fill("Details remote");
    await dialog.getByLabel("Host", { exact: true }).fill(url.hostname);
    await dialog.getByLabel("Port", { exact: true }).fill(url.port);
    await dialog.getByLabel("Use TLS", { exact: true }).uncheck();
    await dialog.getByLabel("Username", { exact: true }).fill(remote.admin.username);
    await dialog.getByLabel("Password", { exact: true }).fill(remote.admin.password);
    await dialog.getByTestId("add-server-connect").click();
    await expect(dialog).toHaveCount(0);
    const item = page
      .getByRole("tree", { name: "Servers" })
      .getByRole("treeitem", { name: "Details remote", exact: true });
    const catalog = item.getByRole("treeitem", { name: "e2e", exact: true });
    await catalog.getByRole("button", { name: "e2e", exact: true }).click();
    const main = catalog.getByRole("treeitem", { name: "main", exact: true });
    await main.getByRole("button", { name: "main", exact: true }).click();
    const row = main.getByRole("treeitem", { name: "remote_details", exact: true }).locator(":scope > .tree-row-wrap");
    await row.hover();
    await row.getByRole("button", { name: /^More for/ }).click();
    await page.getByRole("menuitem", { name: "Preview data" }).click();
    await expect(activeTab(page).getByTestId("tab-server")).toHaveText("Details remote");
    await expect(activeTab(page).getByTestId("results-grid")).toBeVisible();
    await runDone(page, "SET hardcharger.capture_query_profile = 'detailed'");
    await runDone(page, "SELECT sum(id) AS remote_details_probe FROM remote_details");
    const action = activeTab(page).getByRole("link", { name: "SQL details" });
    await expect(action).toHaveAttribute("href", /&server=/);
    const opened = page.waitForEvent("popup");
    await action.click();
    const details = await opened;
    const check = await watchPage(details);
    await expect(details.locator(".chip")).toContainText("Details remote");
    await expect(details.getByRole("region", { name: "Statement details" })).toContainText("remote_details_probe");
    await expect(details.getByTestId("query-profile-plan")).toContainText("UNGROUPED_AGGREGATE");
    await details.reload();
    await expect(details.getByRole("region", { name: "Statement details" })).toContainText("remote_details_probe");
    await details.close();
    check();
  } finally {
    await page.goto("about:blank");
    await remote.stop();
  }
});
