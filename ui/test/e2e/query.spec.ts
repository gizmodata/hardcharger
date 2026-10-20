import { editorText, expect, expectProblem, gridRows, runSql, setSql, signIn, test, editorTabs } from "./fixtures.ts";

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
  await page.locator(".editor-tab:not([hidden]) .view-line").first().click();
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
  // WebKit on macOS: "The network connection was lost", on Linux: "Connection terminated
  // unexpectedly"). The UI handles it; the test checks how.
  expectProblem(page, {
    console:
      /ERR_INCOMPLETE_CHUNKED_ENCODING|Failed to load resource: (The network connection was lost|Connection terminated unexpectedly)/,
    required: false,
  });
  await runSql(page, "SELECT sum(a.range * b.range) AS s FROM range(100000000) a, range(100000) b");
  await expect(page.getByTestId("cancel-button")).toBeVisible();
  await expect(page.getByTestId("results-status")).toContainText("Running");
  await page.waitForTimeout(500);
  await page.getByTestId("cancel-button").click();
  await expect(page.getByTestId("tab-notice")).toContainText("The query was cancelled", { timeout: 15_000 });
  await expect(page.getByTestId("results-status")).toContainText("Cancelled after");
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
  await expect(editorTabs(page).getByRole("tab", { selected: true })).toHaveText("Query 2");
  await runSql(page, "SELECT 'two' AS tab");
  expect((await gridRows(page))[1]).toEqual(["two"]);
  await editorTabs(page).getByRole("tab", { name: "Query 1" }).click();
  expect((await gridRows(page))[1]).toEqual(["one"]);
  // Its text isn't saved anywhere: closing it asks first.
  page.once("dialog", (d) => void d.accept());
  await page.getByRole("button", { name: "Close Query 2" }).click();
  await expect(editorTabs(page).getByRole("tab")).toHaveCount(1);
});

test("the row limit caps what is read and says so", async ({ page }) => {
  await page.getByLabel("Row limit").selectOption("1000");
  await runSql(page, "SELECT * FROM sales.orders ORDER BY id");
  await expect(page.getByTestId("results-status")).toContainText("1,000 rows in");
  await expect(page.getByTestId("truncated")).toHaveText("truncated at 1,000 rows");
  // The grid holds them all, drawing only the rows in view.
  const grid = page.getByTestId("results-grid");
  await grid.evaluate((el) => (el.scrollTop = el.scrollHeight));
  await expect(grid.getByRole("rowheader", { name: "1,000", exact: true })).toBeVisible();
  await page.getByLabel("Row limit").selectOption("10000");
});

// The editor's own text comes back to it as its value after React renders; a key pressed
// before that render's effect ran once had the older text put back (keys lost, the cursor
// moved to the start). Typing as fast as a script can makes that window certain to be hit.
test("fast typing keeps every key", async ({ page }) => {
  const sql = "SELECT version(), current_database(), 42 AS answer FROM range(10) WHERE range > 3 ORDER BY 1";
  for (let i = 0; i < 3; i++) {
    await setSql(page, "");
    await page.keyboard.type(sql);
    await expect.poll(() => editorText(page)).toBe(sql);
    // And it stays so (no late echo puts an older text back).
    await page.waitForTimeout(300);
    expect(await editorText(page)).toBe(sql);
  }
});

// Enter keeps the line's indentation, but typed or pasted text keeps exactly its own: it was
// indented once more on every line (Enter's indentation plus the text's own spaces).
const indented = "SELECT id,\n    customer\n  FROM sales.orders\n    WHERE id < 3;";

/** Puts `text` on the clipboard the way a person would: select it in a text box and copy. */
async function copyText(page: import("@playwright/test").Page, text: string): Promise<void> {
  await page.evaluate((t) => {
    const box = document.createElement("textarea");
    box.id = "copy-source";
    box.value = t;
    document.body.appendChild(box);
    // Selecting a textarea does not focus it in every browser. Safari must copy
    // from this element rather than the Monaco editor that was focused before it.
    box.focus();
    box.select();
  }, text);
  await page.keyboard.press("ControlOrMeta+C");
  await page.evaluate(() => document.getElementById("copy-source")?.remove());
}

test("typed and pasted SQL keeps its own indentation; Enter keeps the line's", async ({ page }) => {
  // Typed key by key.
  await setSql(page, "");
  await page.keyboard.type(indented);
  await expect.poll(() => editorText(page)).toBe(indented);

  // Pasted into an empty editor, and after an Enter that kept an indentation.
  await copyText(page, indented);
  await setSql(page, "");
  await page.keyboard.press("ControlOrMeta+V");
  await expect.poll(() => editorText(page)).toBe(indented);
  await copyText(page, "  WHERE id > 1\n  LIMIT 2");
  await setSql(page, "SELECT *\n  FROM sales.orders");
  await page.keyboard.press("Enter");
  await page.keyboard.press("ControlOrMeta+V");
  await expect.poll(() => editorText(page)).toBe("SELECT *\n  FROM sales.orders\n  WHERE id > 1\n  LIMIT 2");

  // Enter alone keeps the line's indentation, for text that doesn't bring its own.
  await setSql(page, "SELECT id\n    FROM sales.orders");
  await page.keyboard.press("Enter");
  await page.keyboard.type("WHERE id < 2");
  await expect.poll(() => editorText(page)).toBe("SELECT id\n    FROM sales.orders\n    WHERE id < 2");

  // And the text runs as typed.
  await setSql(page, "");
  await page.keyboard.type(indented);
  await page.keyboard.press("ControlOrMeta+Enter");
  await expect.poll(async () => (await gridRows(page)).length).toBe(4);
});
