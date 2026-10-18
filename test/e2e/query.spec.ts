import { expect, expectProblem, gridRows, runSql, setSql, signIn, test } from "./fixtures.ts";

test.beforeEach(async ({ page, harness }) => {
  await signIn(page, harness.admin);
});

test("Ctrl/⌘+Enter runs the statement under the cursor and shows its rows", async ({ page }) => {
  await setSql(
    page,
    "SELECT 1 AS first_one;\nSELECT 'b' AS letter, 2.50::DECIMAL(5,2) AS price, NULL::INTEGER AS nothing;",
  );
  await page.keyboard.press("ControlOrMeta+Enter"); // the cursor is in the second statement
  const rows = await gridRows(page);
  expect(rows[0]).toEqual(["letter", "price", "nothing"]);
  expect(rows[1]).toEqual(["b", "2.50", "NULL"]);
  await expect(page.getByTestId("results-status")).toContainText("1 row in");

  // Now the first statement: the cursor goes to its line.
  await page.locator(".editor-slot:not([hidden]) .view-line").first().click();
  await page.keyboard.press("ControlOrMeta+Enter");
  await expect.poll(async () => (await gridRows(page))[0]).toEqual(["first_one"]);
});

test("the Run button runs a selection; values of many types read as in DuckDB", async ({ page }) => {
  await setSql(
    page,
    "SELECT DATE '2026-10-18' AS d, TIMESTAMP '2026-10-18 01:02:03.25' AS ts, INTERVAL 3 DAY AS iv, " +
      "[1, 2] AS l, {'k': 'v'} AS s, 12345678901234567890::HUGEINT AS big, 'x'::BLOB AS b",
  );
  await page.getByTestId("run-button").click();
  const rows = await gridRows(page);
  expect(rows[1]).toEqual([
    "2026-10-18",
    "2026-10-18 01:02:03.25",
    "3 days",
    "[1, 2]",
    "{'k': v}",
    "12345678901234567890",
    "x",
  ]);
});

test("a statement that changes data reports its row count", async ({ page }) => {
  await runSql(page, "CREATE OR REPLACE TEMP TABLE scratch (id INTEGER)");
  await expect(page.getByTestId("query-message")).toHaveText("Done.");
  await runSql(page, "INSERT INTO scratch SELECT range FROM range(3)");
  await expect(page.getByTestId("query-message")).toHaveText("3 rows changed.");
  await runSql(page, "UPDATE scratch SET id = id + 10 WHERE id < 2");
  await expect(page.getByTestId("query-message")).toHaveText("2 rows changed.");
});

test("an error shows the server's message", async ({ page }) => {
  expectProblem(page, { status: 400, url: /\/statements$/ });
  await runSql(page, "SELECT * FROM no_such_table_here");
  const error = page.getByTestId("query-error");
  await expect(error).toContainText("sql error");
  await expect(error).toContainText("Table with name no_such_table_here does not exist");
  // Positive twin: the same tab runs the next statement.
  await runSql(page, "SELECT 7 AS seven");
  expect((await gridRows(page))[1]).toEqual(["7"]);
});

test("Cancel stops a running query on the server", async ({ page }) => {
  // The server ends the cancelled statement's result stream without its last chunk (Warp SQL
  // §6.6), and browsers log that cut as a failed load (Chromium: ERR_INCOMPLETE_CHUNKED_ENCODING;
  // WebKit: "the network connection was lost"). The UI handles it; the test checks how.
  expectProblem(page, {
    console: /ERR_INCOMPLETE_CHUNKED_ENCODING|Failed to load resource: The network connection was lost/,
    required: false,
  });
  await runSql(page, "SELECT sum(a.range * b.range) AS s FROM range(100000000) a, range(100000) b");
  await expect(page.getByTestId("cancel-button")).toBeVisible();
  await expect(page.getByTestId("results-status")).toContainText("Running");
  await page.waitForTimeout(500);
  await page.getByTestId("cancel-button").click();
  await expect(page.getByTestId("query-message")).toContainText("Cancelled after", { timeout: 15_000 });
  // The tab's session runs one statement at a time: an answer within seconds means the server
  // stopped the long one (it would run for hours).
  const started = Date.now();
  await runSql(page, "SELECT 42 AS answer");
  expect((await gridRows(page))[1]).toEqual(["42"]);
  expect(Date.now() - started).toBeLessThan(10_000);
});

test("tabs keep their own SQL and results", async ({ page }) => {
  await runSql(page, "SELECT 'one' AS tab");
  expect((await gridRows(page))[1]).toEqual(["one"]);
  await page.getByRole("button", { name: "New tab" }).click();
  await expect(page.getByRole("tab", { selected: true })).toHaveText("Query 2");
  await runSql(page, "SELECT 'two' AS tab");
  expect((await gridRows(page))[1]).toEqual(["two"]);
  await page.getByRole("tab", { name: "Query 1" }).click();
  expect((await gridRows(page))[1]).toEqual(["one"]);
  await page.getByRole("button", { name: "Close Query 2" }).click();
  await expect(page.getByRole("tab")).toHaveCount(1);
});

test("the row limit caps what is read and says so", async ({ page }) => {
  await page.getByLabel("Row limit").selectOption("1000");
  await runSql(page, "SELECT * FROM sales.orders ORDER BY id");
  await expect(page.getByTestId("results-status")).toContainText("1,000 rows in");
  await expect(page.getByTestId("results-status")).toContainText("the first 1,000; more were not read");
  // The grid holds them all, drawing only the rows in view.
  const grid = page.getByTestId("results-grid");
  await grid.evaluate((el) => (el.scrollTop = el.scrollHeight));
  await expect(grid.getByRole("rowheader", { name: "1000", exact: true })).toBeVisible();
  await page.getByLabel("Row limit").selectOption("10000");
});
