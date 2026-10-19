// What Run, Run all and their shortcuts execute: the statement at the cursor (marked in the
// margin), exactly the selection, or every statement in order with a result tab each and
// Messages, stopping at the first error.
import { activeTab, editorFocused, expect, expectProblem, gridRows, runSql, setSql, signIn, test } from "./fixtures.ts";

test.beforeEach(async ({ page, harness }) => {
  await signIn(page, harness.admin);
  await editorFocused(page);
});

const lines = (page: import("@playwright/test").Page) => activeTab(page).locator(".view-line");

test("the margin bar marks the statement Run executes; the selection runs exactly", async ({ page }) => {
  const tab = activeTab(page);
  await setSql(page, "SELECT 1 AS one;\nSELECT 2 AS two,\n  'b' AS letter;\nSELECT 3 AS three;");
  // One statement: no bar. Three: the bar is on the one the cursor is in (the last).
  await expect(tab.locator(".editor-current-statement")).toHaveCount(1);
  await expect(tab.getByTestId("run-all-button")).toHaveText("Run all (3)");
  await lines(page).nth(1).click();
  await expect(tab.locator(".editor-current-statement")).toHaveCount(2); // its two lines
  await page.keyboard.press("ControlOrMeta+Enter");
  await expect.poll(async () => (await gridRows(page))[0]).toEqual(["two", "letter"]);

  // Highlighted text wins over the statement at the cursor: here, part of the first line.
  await lines(page).nth(0).click();
  await page.keyboard.press("Home");
  await page.keyboard.press("Shift+End");
  await expect(tab.locator(".editor-current-statement")).toHaveCount(0);
  await tab.getByTestId("run-button").click();
  await expect.poll(async () => (await gridRows(page))[0]).toEqual(["one"]);
});

test("Run all runs every statement: a result tab for each result set, and Messages", async ({ page }) => {
  const tab = activeTab(page);
  await setSql(
    page,
    "CREATE OR REPLACE TEMP TABLE runall (i INTEGER);\n" +
      "INSERT INTO runall VALUES (1), (2), (3);\n" +
      "SELECT count(*) AS n FROM runall;\n" +
      "SELECT i FROM runall ORDER BY i;",
  );
  await page.keyboard.press("ControlOrMeta+Shift+Enter");
  const tabs = tab.getByRole("tablist", { name: "Result sets" }).getByRole("tab");
  await expect(tabs).toHaveCount(3);
  await expect(tabs.nth(0)).toContainText("Result 1");
  await expect(tabs.nth(1)).toHaveAttribute("aria-selected", "true"); // the last result set
  expect((await gridRows(page)).map((r) => r[0])).toEqual(["i", "1", "2", "3"]);
  await tabs.nth(0).click();
  expect(await gridRows(page)).toEqual([["n"], ["3"]]);
  await tab.getByRole("tab", { name: "Messages" }).click();
  const messages = tab.getByTestId("messages").getByRole("listitem");
  await expect(messages).toHaveCount(4);
  await expect(messages.nth(1)).toContainText("3 rows affected");
  await expect(messages.nth(3)).toContainText("3 rows");
  await expect(tab.getByTestId("results-status")).toContainText("4 statements · 2 result sets in");
});

test("Run all stops at the first error; the rest don't run", async ({ page }) => {
  expectProblem(page, { status: 400, url: /\/statements$/ });
  const tab = activeTab(page);
  await setSql(
    page,
    "CREATE OR REPLACE TEMP TABLE stops (i INTEGER);\nSELECT nope FROM stops;\nINSERT INTO stops VALUES (1);",
  );
  await tab.getByTestId("run-all-button").click();
  await expect(tab.getByTestId("query-error")).toContainText("Statement 2 of 3 failed");
  await expect(tab.getByTestId("query-error")).toContainText('Referenced column "nope" not found');
  await tab.getByRole("tab", { name: "Messages" }).click();
  const messages = tab.getByTestId("messages").getByRole("listitem");
  await expect(messages.nth(1)).toContainText("failed");
  await expect(messages.nth(2)).toContainText("not run");
  // The INSERT never ran.
  await runSql(page, "SELECT count(*) AS n FROM stops");
  expect((await gridRows(page))[1]).toEqual(["0"]);
});

test("a script keeps at most 50 result sets; the rest run and are listed", async ({ page }) => {
  const tab = activeTab(page);
  const script = Array.from({ length: 52 }, (_, i) => `SELECT ${i + 1} AS n;`).join("\n");
  await setSql(page, script);
  await tab.getByTestId("run-all-button").click();
  const tabs = tab.getByRole("tablist", { name: "Result sets" }).getByRole("tab");
  await expect(tabs).toHaveCount(51); // 50 results and Messages
  await expect(tabs.nth(49)).toHaveAttribute("aria-selected", "true");
  expect((await gridRows(page))[1]).toEqual(["50"]);
  await tab.getByRole("tab", { name: "Messages" }).click();
  const messages = tab.getByTestId("messages").getByRole("listitem");
  await expect(messages).toHaveCount(52);
  await expect(messages.nth(50)).toContainText("1 row, not kept");
  await expect(messages.nth(51)).toContainText("1 row, not kept");
});
