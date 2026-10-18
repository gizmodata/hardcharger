import { readFileSync } from "node:fs";

import type { Page } from "@playwright/test";
import { tableFromIPC } from "apache-arrow";
import { readParquet } from "parquet-wasm";

import { expect, gridRows, runSql, signIn, test } from "./fixtures.ts";

async function exportAs(page: Page, label: string): Promise<{ name: string; bytes: Buffer }> {
  await page.getByTestId("export-button").click();
  const download = page.waitForEvent("download");
  await page.getByRole("menuitem", { name: label }).click();
  const d = await download;
  const file = await d.path();
  return { name: d.suggestedFilename(), bytes: readFileSync(file) };
}

test.beforeEach(async ({ page, harness }) => {
  await signIn(page, harness.admin);
  await runSql(page, "SELECT * FROM sales.tricky ORDER BY id");
  await expect.poll(async () => (await gridRows(page)).length).toBe(4);
});

test("CSV and TSV exports quote what needs quoting; NULL is empty", async ({ page }) => {
  const csv = await exportAs(page, "CSV (comma-separated)");
  expect(csv.name).toMatch(/^results-\d{8}-\d{6}\.csv$/);
  expect(csv.bytes.toString()).toBe('id,label,note\n1,plain,nothing odd\n2,tab\there,"say ""hi"""\n3,,"line\nbreak"\n');
  const tsv = await exportAs(page, "TSV (tab-separated)");
  expect(tsv.name).toMatch(/\.tsv$/);
  expect(tsv.bytes.toString()).toBe(
    'id\tlabel\tnote\n1\tplain\tnothing odd\n2\t"tab\there"\t"say ""hi"""\n3\t\t"line\nbreak"\n',
  );
});

test("the JSON export keeps numbers exact", async ({ page }) => {
  await runSql(
    page,
    "SELECT 12345678901234567890::HUGEINT AS big, 0.10::DECIMAL(4,2) AS cents, NULL AS n, [1, 2] AS l",
  );
  await expect.poll(async () => (await gridRows(page))[0]).toEqual(["big", "cents", "n", "l"]);
  const json = await exportAs(page, "JSON");
  expect(json.name).toMatch(/\.json$/);
  const text = json.bytes.toString();
  expect(text).toContain('"big":12345678901234567890');
  expect(text).toContain('"cents":0.10');
  expect(JSON.parse(text)).toEqual([{ big: Number("12345678901234567890"), cents: 0.1, n: null, l: [1, 2] }]);
});

test("the Parquet export (WebAssembly, under the server's CSP) reads back the same rows", async ({ page }) => {
  const parquet = await exportAs(page, "Parquet");
  expect(parquet.name).toMatch(/\.parquet$/);
  expect(parquet.bytes.subarray(0, 4).toString()).toBe("PAR1");
  const table = tableFromIPC(readParquet(new Uint8Array(parquet.bytes)).intoIPCStream());
  expect(table.numRows).toBe(3);
  expect(table.toArray().map((r) => r.toJSON())).toEqual([
    { id: 1, label: "plain", note: "nothing odd" },
    { id: 2, label: "tab\there", note: 'say "hi"' },
    { id: 3, label: null, note: "line\nbreak" },
  ]);
  await expect(page.getByTestId("results-notice")).toHaveText("Exported 3 rows as PARQUET.");
});
