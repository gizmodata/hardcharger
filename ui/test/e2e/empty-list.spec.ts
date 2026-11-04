// A query that returns no rows with a LIST column (gizmodata/hardcharger#23): the client's byte
// count of the one empty batch apache-arrow makes walked into a LIST's missing child and threw
// "Cannot read properties of undefined (reading 'byteLength')". It reads as 0 rows now.
import { editorFocused, expect, gridRows, runDone, signIn, test } from "./fixtures.ts";

test.beforeEach(async ({ page, harness }) => {
  await signIn(page, harness.admin);
  await editorFocused(page);
});

for (const [label, sql] of [
  ["a LIST", "SELECT ['a'] AS v FROM range(3) WHERE range > 10"],
  ["a LIST of STRUCTs", "SELECT [{'seq': 1, 'at': 'x'}] AS problems FROM range(3) WHERE range > 10"],
  ["a STRUCT with a LIST in it", "SELECT {'tags': ['a'], 'n': 1} AS s FROM range(3) WHERE range > 10"],
] as const) {
  test(`no rows with ${label} column reads as 0 rows`, async ({ page }) => {
    await runDone(page, sql);
    await expect(page.getByTestId("query-error")).toHaveCount(0);
    await expect(page.getByText("0 rows", { exact: false }).first()).toBeVisible();
    expect(await gridRows(page)).toHaveLength(1); // the header alone
    // The positive twin: with a row, the value shows.
    await runDone(page, sql.replace("range > 10", "range = 1"));
    expect((await gridRows(page)).length).toBe(2);
  });
}
