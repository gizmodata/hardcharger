import { expect, gridRows, pasted, runSql, signIn, test } from "./fixtures.ts";

test("Ctrl/⌘+C copies the selected cells as tab-separated text", async ({ page, harness }) => {
  await signIn(page, harness.admin);
  await runSql(page, "SELECT * FROM sales.tricky ORDER BY id");
  await expect.poll(async () => (await gridRows(page)).length).toBe(4);
  const grid = page.getByTestId("results-grid");
  // From "plain" (row 1, label) to the last row's note, with shift-click.
  await grid.getByRole("gridcell", { name: "plain", exact: true }).click();
  await grid.getByRole("gridcell", { name: /^line/ }).click({ modifiers: ["Shift"] });
  await expect(grid.locator(".grid-selected[role=gridcell]")).toHaveCount(6);
  await page.keyboard.press("ControlOrMeta+C");
  await expect(page.getByTestId("results-notice")).toHaveText("Copied 6 cells.");
  expect(await pasted(page)).toBe('plain\tnothing odd\n"tab\there"\t"say ""hi"""\n\t"line\nbreak"');

  // The arrow keys move the selection; shift extends it.
  await grid.getByRole("gridcell", { name: "1", exact: true }).click();
  await page.keyboard.press("Shift+ArrowRight");
  await page.keyboard.press("ControlOrMeta+C");
  expect(await pasted(page)).toBe("1\tplain");
});

test("the Copy button copies every row with its headers", async ({ page, harness }) => {
  await signIn(page, harness.admin);
  await runSql(page, "SELECT id, label FROM sales.tricky ORDER BY id");
  await expect.poll(async () => (await gridRows(page)).length).toBe(4);
  await page.getByRole("button", { name: "Copy", exact: true }).click();
  await expect(page.getByTestId("results-notice")).toHaveText("Copied 3 rows with headers.");
  expect(await pasted(page)).toBe('id\tlabel\n1\tplain\n2\t"tab\there"\n3\t\n');
});
