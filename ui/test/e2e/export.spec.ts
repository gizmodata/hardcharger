import { readFileSync } from "node:fs";
import { inflateRawSync } from "node:zlib";

import type { Page } from "@playwright/test";
import { tableFromIPC } from "apache-arrow";
import { readParquet } from "parquet-wasm";

import { activeTab, expect, gridRows, runDone, runSql, signIn, test } from "./fixtures.ts";

async function exportAs(page: Page, label: string): Promise<{ name: string; bytes: Buffer }> {
  await activeTab(page).getByTestId("export-button").click();
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

/** The files of a zip (an .xlsx), read back. */
function unzip(bytes: Buffer): Map<string, string> {
  const out = new Map<string, string>();
  let at = 0;
  while (bytes.readUInt32LE(at) === 0x04034b50) {
    const method = bytes.readUInt16LE(at + 8);
    const size = bytes.readUInt32LE(at + 18);
    const nameLength = bytes.readUInt16LE(at + 26);
    const name = bytes.subarray(at + 30, at + 30 + nameLength).toString();
    const body = bytes.subarray(at + 30 + nameLength, at + 30 + nameLength + size);
    out.set(name, (method === 8 ? inflateRawSync(body) : body).toString());
    at += 30 + nameLength + size;
  }
  return out;
}

test("CSV, TSV, JSON and Excel hold the rows as shown (filtered and sorted)", async ({ page }) => {
  const tab = activeTab(page);
  await tab.getByTestId("results-filter").fill("odd"); // row 1 only
  await tab.getByRole("columnheader", { name: /^id/ }).getByRole("button").click();
  const csv = await exportAs(page, "CSV (comma-separated)");
  expect(csv.bytes.toString()).toBe("id,label,note\n1,plain,nothing odd\n");
  await tab.getByTestId("results-filter").fill("");
  await tab.getByRole("columnheader", { name: /^id/ }).getByRole("button").click(); // descending
  const json = await exportAs(page, "JSON");
  expect((JSON.parse(json.bytes.toString()) as { id: number }[]).map((r) => r.id)).toEqual([3, 2, 1]);
  const xlsx = await exportAs(page, "Excel (.xlsx)");
  expect(xlsx.name).toMatch(/\.xlsx$/);
  const sheet = unzip(xlsx.bytes).get("xl/worksheets/sheet1.xml")!;
  const ids = [...sheet.matchAll(/<c r="A(\d+)"><v>(\d+)<\/v><\/c>/g)].map((m) => m[2]);
  expect(ids).toEqual(["3", "2", "1"]);
  expect(sheet).toContain('<t xml:space="preserve">line\nbreak</t>');
  await expect(page.getByTestId("results-notice")).toHaveText("Exported 3 rows as XLSX.");
});

test("Arrow IPC holds the full result with the server's exact types", async ({ page }) => {
  await runDone(
    page,
    "SELECT 12345678901234567890::HUGEINT AS big, 0.10::DECIMAL(4,2) AS cents, TIMESTAMP '2026-10-18 01:02:03.25' AS ts, [1, 2] AS l",
  );
  const arrow = await exportAs(page, "Arrow IPC (.arrows)");
  expect(arrow.name).toMatch(/\.arrows$/);
  const table = tableFromIPC(new Uint8Array(arrow.bytes));
  expect(table.schema.fields.map((f) => [f.name, String(f.type)])).toEqual([
    ["big", "Decimal[38e0]"],
    ["cents", "Decimal[4e+2]"],
    ["ts", "Timestamp<MICROSECOND>"],
    ["l", "List<Int32>"],
  ]);
  expect(table.numRows).toBe(1);
});
