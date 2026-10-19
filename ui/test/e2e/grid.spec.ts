// The results grid: sort (NULLs last), filter, column resizing, the spreadsheet-like selection
// and its keyboard, copy that pastes into Excel and Google Sheets as cells, the cell inspector,
// and Expand / Restore.
import type { Page } from "@playwright/test";

import { activeTab, editorFocused, expect, gridRows, pastedBoth, runDone, signIn, test } from "./fixtures.ts";

test.beforeEach(async ({ page, harness }) => {
  await signIn(page, harness.admin);
  await editorFocused(page);
});

// 60 rows × 20 columns: leading-zero text, a value with a line break, a tab and quotes, a NULL.
const extra = Array.from(
  { length: 14 },
  (_, k) => `'row-' || i || '-col-${k + 6}-padding' AS c${String(k + 6).padStart(2, "0")}`,
);
const wide =
  "SELECT i AS id, lpad(i::VARCHAR, 5, '0') AS code, 'name ' || i AS name, i + 0.25 AS amount, " +
  "CASE WHEN i = 2 THEN 'line one' || chr(10) || 'line' || chr(9) || '\"two\"' ELSE 'note ' || i END AS note, " +
  "CASE WHEN i = 3 THEN NULL ELSE i * 10 END AS maybe, " +
  extra.join(", ") +
  " FROM range(1, 61) t(i) ORDER BY i";

function grid(page: Page) {
  return activeTab(page).getByTestId("results-grid");
}
/** The cell at a shown row (1-based) and column (0-based). */
function cell(page: Page, row: number, col: number) {
  return grid(page).locator(`[role="row"][aria-rowindex="${row + 1}"] [role="gridcell"][aria-colindex="${col + 2}"]`);
}
const selection = (page: Page) => activeTab(page).getByTestId("results-selection");
const notice = (page: Page) => activeTab(page).getByTestId("results-notice");
const AMOUNT = 3;
const MAYBE = 5;
// Rows 2-3 × amount..maybe: the multi-line value with a tab and quotes, and a NULL (an empty cell).
const rangeTsv = '2.25\t"line one\nline\t""two"""\t20\n3.25\tnote 3\t';

test("cells select like a spreadsheet; the copy pastes into Excel as cells", async ({ page }) => {
  test.setTimeout(120_000);
  await runDone(page, wide);
  const active = grid(page).locator('[role="gridcell"][data-active]');

  // One click selects (no dialog); the arrow keys move; one cell copies as its bare value.
  await grid(page).getByText("00001", { exact: true }).click();
  await expect(page.getByTestId("cell-inspector")).toHaveCount(0);
  await expect(cell(page, 1, 1)).toHaveAttribute("aria-selected", "true");
  await expect(grid(page)).toBeFocused();
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("ArrowDown");
  await expect(active).toHaveText("name 2");
  await page.keyboard.press("ArrowUp");
  await page.keyboard.press("ArrowLeft");
  await expect(active).toHaveText("00001");
  await page.keyboard.press("ControlOrMeta+C");
  await expect(notice(page)).toHaveText("Copied 1 cell.");
  const one = await pastedBoth(page);
  expect(one.text).toBe("00001");
  expect(one.html).toContain("mso-number-format"); // a text column: Excel keeps the leading zeros

  // Shift and the arrow keys: rows 2-3 × amount..maybe.
  await cell(page, 2, AMOUNT).click();
  await page.keyboard.press("Shift+ArrowRight");
  await page.keyboard.press("Shift+ArrowRight");
  await page.keyboard.press("Shift+ArrowDown");
  await expect(selection(page)).toHaveText("· 2 rows × 3 columns selected");
  await expect(cell(page, 3, MAYBE)).toHaveAttribute("aria-selected", "true");
  await expect(cell(page, 1, AMOUNT)).toHaveAttribute("aria-selected", "false");
  await page.keyboard.press("ControlOrMeta+C");
  await expect(notice(page)).toHaveText("Copied 6 cells.");
  const range = await pastedBoth(page);
  expect(range.text).toBe(rangeTsv);
  expect(range.html).toContain("<td>2.25</td>");
  expect(range.html).toContain("line one<br"); // the line break stays in one cell
  expect(range.html).toMatch(/<td[^>]*><\/td><\/tr>/); // the NULL is an empty cell

  // The same range with a mouse drag, then with Shift+click.
  await cell(page, 1, 0).click();
  const from = (await cell(page, 2, AMOUNT).boundingBox())!;
  const to = (await cell(page, 3, MAYBE).boundingBox())!;
  await page.mouse.move(from.x + 5, from.y + from.height / 2);
  await page.mouse.down();
  await page.mouse.move(to.x + 5, to.y + to.height / 2, { steps: 8 });
  await page.mouse.up();
  await expect(selection(page)).toHaveText("· 2 rows × 3 columns selected");
  await page.keyboard.press("ControlOrMeta+C");
  expect((await pastedBoth(page)).text).toBe(rangeTsv);
  await cell(page, 2, AMOUNT).click();
  await cell(page, 3, MAYBE).click({ modifiers: ["Shift"] });
  await expect(selection(page)).toHaveText("· 2 rows × 3 columns selected");

  // Ctrl/⌘+End goes to the last cell (scrolling it into view: rows are drawn only in view),
  // Page Up a screenful, Ctrl/⌘+Home back; Home and End go to the row's ends.
  await grid(page).focus();
  await page.keyboard.press("ControlOrMeta+End");
  await expect(active).toHaveText("row-60-col-19-padding");
  await expect(active).toBeInViewport();
  await page.keyboard.press("PageUp");
  await expect(active).toBeInViewport();
  await page.keyboard.press("ControlOrMeta+Home");
  await expect(active).toHaveText("1");
  await expect(active).toBeInViewport();
  await page.keyboard.press("End");
  await expect(active).toHaveText("row-1-col-19-padding");
  await page.keyboard.press("Home");
  await expect(active).toHaveText("1");

  // A drag past the bottom edge keeps selecting while the grid scrolls.
  const top = (await cell(page, 1, 0).boundingBox())!;
  const box = (await grid(page).boundingBox())!;
  await page.mouse.move(top.x + 5, top.y + top.height / 2);
  await page.mouse.down();
  await page.mouse.move(top.x + 5, box.y + box.height + 40, { steps: 6 });
  await page.waitForTimeout(800);
  await page.mouse.up();
  await expect(selection(page)).toHaveText(/rows × 1 column selected/);
  const dragged = Number((await selection(page).textContent())!.match(/([\d,]+) rows/)![1]!.replaceAll(",", ""));
  expect(dragged).toBeGreaterThan(Math.floor(box.height / 26));

  // Row numbers select whole rows; drag down them for several.
  const rowNumber = (row: number) => grid(page).locator(`[role="row"][aria-rowindex="${row + 1}"] [role="rowheader"]`);
  await grid(page).evaluate((el) => (el.scrollTop = 0));
  await rowNumber(2).click();
  await expect(selection(page)).toHaveText("· 1 row × 20 columns selected");
  const r2 = (await rowNumber(2).boundingBox())!;
  const r4 = (await rowNumber(4).boundingBox())!;
  await page.mouse.move(r2.x + 10, r2.y + r2.height / 2);
  await page.mouse.down();
  await page.mouse.move(r4.x + 10, r4.y + r4.height / 2, { steps: 4 });
  await page.mouse.up();
  await expect(selection(page)).toHaveText("· 3 rows × 20 columns selected");

  // Ctrl/⌘+A selects every row shown; Escape keeps only the active cell; a sort starts over.
  await page.keyboard.press("ControlOrMeta+A");
  await expect(selection(page)).toHaveText("· 60 rows × 20 columns selected");
  await page.keyboard.press("Escape");
  await expect(selection(page)).toHaveCount(0);
  await expect(grid(page).locator('[aria-selected="true"]')).toHaveCount(1);
  await grid(page)
    .getByRole("button", { name: /^amount/ })
    .click();
  await expect(grid(page).locator('[aria-selected="true"]')).toHaveCount(0);
});

test("double-click or Enter shows a value in full, JSON pretty-printed; Esc keeps the selection", async ({ page }) => {
  await runDone(
    page,
    "SELECT 'line one' || chr(10) || 'line two' AS note, {'a': 1, 'b': [1, 2], 'big': 123456789012345678901234567890::HUGEINT} AS nested, " +
      '\'{"k": [1, {"x": null}], "n": 12345678901234567890}\' AS json_text, NULL::VARCHAR AS nothing',
  );
  const dialog = page.getByTestId("cell-inspector");
  await cell(page, 1, 1).dblclick();
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("heading")).toContainText("nested");
  await expect(dialog.getByTestId("inspector-value")).toHaveText(
    '{\n  "a": 1,\n  "b": [\n    1,\n    2\n  ],\n  "big": 123456789012345678901234567890\n}',
  );
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(cell(page, 1, 1)).toHaveAttribute("aria-selected", "true");
  await expect(grid(page)).toBeFocused();

  // A text column holding JSON: pretty-printed, its big number exact.
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("Enter");
  await expect(dialog.getByTestId("inspector-value")).toHaveText(
    '{\n  "k": [\n    1,\n    {\n      "x": null\n    }\n  ],\n  "n": 12345678901234567890\n}',
  );
  await dialog.getByRole("button", { name: "Copy value" }).click();
  await expect(dialog.getByRole("button", { name: "Copied" })).toBeVisible();
  await page.keyboard.press("Escape");

  // Plain text keeps its line breaks; NULL says so and has nothing to copy.
  await cell(page, 1, 0).dblclick();
  await expect(dialog.getByTestId("inspector-value")).toHaveText("line one\nline two");
  await page.keyboard.press("Escape");
  await cell(page, 1, 3).dblclick();
  await expect(dialog.getByTestId("inspector-value")).toHaveText("NULL");
  await expect(dialog.getByRole("button", { name: "Copy value" })).toBeDisabled();
});

test("sort by a column (again for descending, a third time to clear), NULLs last; filter rows", async ({ page }) => {
  await runDone(
    page,
    "SELECT * FROM (VALUES (1, 'pear', 3.5), (2, NULL, 1.25), (3, 'apple', NULL), (4, 'Banana', 10.0), (5, 'cherry', -2.0)) t(id, fruit, price)",
  );
  const head = (name: string) => grid(page).getByRole("columnheader", { name: new RegExp(`^${name}`) });
  const ids = async () => (await gridRows(page)).slice(1).map((r) => r[0]);
  await head("price").getByRole("button").click();
  await expect(head("price")).toHaveAttribute("aria-sort", "ascending");
  expect(await ids()).toEqual(["5", "2", "1", "4", "3"]); // -2, 1.25, 3.5, 10, NULL
  await head("price").getByRole("button").click();
  expect(await ids()).toEqual(["4", "1", "2", "5", "3"]); // NULL still last
  await head("price").getByRole("button").click();
  await expect(head("price")).not.toHaveAttribute("aria-sort");
  expect(await ids()).toEqual(["1", "2", "3", "4", "5"]);
  await head("fruit").getByRole("button").click();
  expect(await ids()).toEqual(["4", "3", "5", "1", "2"]); // byte order: 'Banana' < 'apple'
  await expect(activeTab(page).getByTestId("results-count")).toHaveText("5 rows · sorted by fruit ascending");

  const filter = activeTab(page).getByTestId("results-filter");
  await filter.fill("an");
  await expect(activeTab(page).getByTestId("results-count")).toHaveText(
    "1 of 5 rows match · sorted by fruit ascending",
  );
  expect(await ids()).toEqual(["4"]);
  await filter.fill("null");
  expect(await ids()).toEqual(["3", "2"]); // the NULLs read as NULL
  await filter.fill("zzz");
  await expect(grid(page)).toContainText("No rows match the filter.");
  await activeTab(page).getByRole("button", { name: "Clear the filter" }).click();
  expect(await ids()).toHaveLength(5);
});

test("columns resize by drag, by keyboard, and fit again on double-click", async ({ page }) => {
  await runDone(page, "SELECT 'short' AS a, 'b' AS b");
  const header = grid(page).getByRole("columnheader", { name: /^a/ });
  const width = async () => (await header.boundingBox())!.width;
  const fitted = await width();
  const handle = grid(page).getByTestId("column-resize-0");
  const box = (await handle.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + 80, box.y + box.height / 2, { steps: 4 });
  await page.mouse.up();
  await expect.poll(width).toBeCloseTo(fitted + 80, 0);
  // A drag never sorts.
  await expect(header).not.toHaveAttribute("aria-sort");
  await handle.focus();
  await page.keyboard.press("ArrowRight");
  await expect.poll(width).toBeCloseTo(fitted + 96, 0);
  await handle.dblclick();
  await expect.poll(width).toBeCloseTo(fitted, 0);
  // The cells follow their header.
  expect((await cell(page, 1, 0).boundingBox())!.width).toBeCloseTo(fitted, 0);
});

test("Expand fills the tab with the results; Restore brings the editor back", async ({ page }) => {
  await runDone(page, "SELECT * FROM range(500)");
  const editor = activeTab(page).locator(".editor-area");
  await expect(editor).toBeVisible();
  const before = (await grid(page).boundingBox())!.height;
  await activeTab(page).getByTestId("results-expand").click();
  await expect(editor).toBeHidden();
  await expect(activeTab(page).getByTestId("results-expand")).toHaveText("Restore");
  expect((await grid(page).boundingBox())!.height).toBeGreaterThan(before + 150);
  await activeTab(page).getByTestId("results-expand").click();
  await expect(editor).toBeVisible();
  await expect.poll(async () => (await grid(page).boundingBox())!.height).toBeCloseTo(before, 0);
});
