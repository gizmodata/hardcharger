// Upload data: files read and converted in the browser (the upload worker) and sent over Warp
// SQL's bulk ingest, in both browsers, under the server's Content-Security-Policy (the
// fixtures fail a test on any violation, console error or failed request it didn't declare).
// Every upload is checked by reading the table back from the server: values, not counts.

import { createWriteStream, writeFileSync } from "node:fs";
import { once } from "node:events";
import os from "node:os";
import path from "node:path";

import { type Connection, connect } from "@gizmodata/warp-sql";
import type { Locator, Page, TestInfo } from "@playwright/test";
import { tableFromArrays, tableToIPC } from "apache-arrow";
import { Table as WasmTable, writeParquet } from "parquet-wasm";

import { buildXlsx, serial } from "../unit/workbook.ts";
import { activeTab, editorFocused, expect, expectProblem, gridRows, signIn, test, watchPage } from "./fixtures.ts";
import type { HarnessState } from "./harness.ts";

async function admin(harness: HarnessState): Promise<Connection> {
  return connect({ url: harness.serverUrl, username: harness.admin.username, password: harness.admin.password });
}

async function rows(harness: HarnessState, sql: string): Promise<Record<string, unknown>[]> {
  const c = await admin(harness);
  try {
    return (await c.query(sql)).toArray().map((r) => (r as { toJSON: () => Record<string, unknown> }).toJSON());
  } finally {
    await c.close();
  }
}

async function tableExists(harness: HarnessState, catalog: string, table: string): Promise<boolean> {
  const found = await rows(
    harness,
    `SELECT count(*) AS n FROM duckdb_tables() WHERE database_name = '${catalog}' AND table_name = '${table}'`,
  );
  return Number(found[0]!["n"]) > 0;
}

function write(testInfo: TestInfo, name: string, data: string | Uint8Array): string {
  const file = testInfo.outputPath(name);
  writeFileSync(file, data);
  return file;
}

/** A unique table name for this browser's run. */
const tableName = (testInfo: TestInfo, base: string) => `${base}_${testInfo.project.name}`;

async function openUpload(page: Page): Promise<Locator> {
  await activeTab(page).getByTestId("upload-button").click();
  const dialog = page.getByTestId("upload-data-dialog");
  await expect(dialog.getByRole("heading", { name: "Upload data" })).toBeVisible();
  return dialog;
}

async function chooseFile(dialog: Locator, file: string, table?: string): Promise<void> {
  await dialog.getByLabel("File", { exact: true }).setInputFiles(file);
  await expect(dialog.getByTestId("upload-preview-toggle")).toBeVisible();
  if (table !== undefined) await dialog.getByLabel("Table", { exact: true }).fill(table);
}

async function uploadAndWait(dialog: Locator): Promise<string> {
  await dialog.getByRole("button", { name: "Upload", exact: true }).click();
  const result = dialog.getByTestId("upload-data-result");
  await expect(result).toBeVisible({ timeout: 120_000 });
  return (await result.textContent()) ?? "";
}

test.beforeEach(async ({ page, harness }) => {
  await signIn(page, harness.admin);
  await editorFocused(page);
});

test("an Excel workbook: sheets, dates, numbers, text and blank cells load with their types", async ({
  page,
  harness,
}, testInfo) => {
  const book = await buildXlsx([
    {
      name: "Orders",
      rows: [
        [{ s: "id" }, { s: "customer" }, { s: "amount" }, { s: "ordered" }, { s: "paid" }, { s: "code" }],
        [
          { n: 1 },
          { s: "Acme" },
          { n: 1234.5, style: "money" },
          { n: serial(2026, 1, 31), style: "date" },
          { b: true },
          { s: "00123" },
        ],
        null,
        [
          { n: 2 },
          { is: "Beta & Co" },
          { f: "A4*10", v: 20 },
          { n: serial(2024, 2, 29), style: "isoDate" },
          { b: false },
          null,
        ],
        [{ n: 3 }, null, null, null, null, { s: "7" }],
      ],
    },
    {
      name: "Shifts",
      rows: [
        [{ s: "who" }, { s: "starts" }, { s: "at" }],
        [{ s: "Ana" }, { n: 0.375, style: "time" }, { n: serial(2026, 10, 20, 9, 0), style: "datetime" }],
      ],
    },
  ]);
  const file = write(testInfo, "Sales Q3 2026.xlsx", book);
  const dialog = await openUpload(page);
  await chooseFile(dialog, file);
  // The table name comes from the file name, as GizmoData Cloud derives it.
  await expect(dialog.getByLabel("Table", { exact: true })).toHaveValue("sales_q3_2026");
  const table = tableName(testInfo, "sales_q3_2026");
  await dialog.getByLabel("Table", { exact: true }).fill(table);
  await expect(dialog.getByLabel("Worksheet")).toBeVisible();
  await expect(dialog.getByLabel("Worksheet").locator("option")).toHaveText([
    "First sheet",
    "Orders (first sheet)",
    "Shifts",
  ]);
  await expect(dialog.getByTestId("upload-notes")).toContainText("1 empty row left out");
  // The preview, folded away until opened: the inferred types, editable.
  await dialog.getByTestId("upload-preview-toggle").click();
  const preview = dialog.getByTestId("upload-preview");
  await expect(preview.getByLabel("Type of column 4")).toHaveValue("DATE");
  await expect(preview.getByLabel("Type of column 6")).toHaveValue("VARCHAR");
  await expect(preview.locator("tbody tr").first()).toContainText("2026-01-31");
  const text = await uploadAndWait(dialog);
  expect(text).toMatch(
    new RegExp(`Loaded 3 rows into ${harness.catalog}\\.main\\.${table} \\(create, XLSX, \\d+\\.\\ds\\)`),
  );
  expect(
    await rows(
      harness,
      `SELECT id, customer, amount, ordered::VARCHAR AS ordered, paid, code FROM main.${table} ORDER BY id`,
    ),
  ).toEqual([
    { id: 1n, customer: "Acme", amount: 1234.5, ordered: "2026-01-31", paid: true, code: "00123" },
    { id: 2n, customer: "Beta & Co", amount: 20, ordered: "2024-02-29", paid: false, code: null },
    { id: 3n, customer: null, amount: null, ordered: null, paid: null, code: "7" },
  ]);
  const types = await rows(
    harness,
    `SELECT column_name, data_type FROM information_schema.columns WHERE table_name = '${table}' ORDER BY ordinal_position`,
  );
  expect(types.map((t) => t["data_type"])).toEqual(["BIGINT", "VARCHAR", "DOUBLE", "DATE", "BOOLEAN", "VARCHAR"]);

  // The second sheet, into a table of its own.
  await dialog.getByRole("button", { name: "Upload another" }).click();
  await chooseFile(dialog, file, `${table}_shifts`);
  await dialog.getByLabel("Worksheet").selectOption("Shifts");
  await expect(dialog.getByTestId("upload-preview-toggle")).toContainText("sheet Shifts");
  await uploadAndWait(dialog);
  expect(
    await rows(harness, `SELECT who, starts::VARCHAR AS starts, "at"::VARCHAR AS "at" FROM main.${table}_shifts`),
  ).toEqual([{ who: "Ana", starts: "09:00:00", at: "2026-10-20 09:00:00" }]);

  // Query this table: a new tab with the table's first rows.
  await dialog.getByRole("button", { name: "Query this table" }).click();
  await expect(dialog).toHaveCount(0);
  await expect.poll(async () => (await gridRows(page))[0]).toEqual(["who", "starts", "at"]);
  expect((await gridRows(page))[1]).toEqual(["Ana", "09:00:00", "2026-10-20 09:00:00"]);
});

test("a CSV: quotes, line breaks, empty fields, mixed types; a type changed in the preview", async ({
  page,
  harness,
}, testInfo) => {
  const file = write(
    testInfo,
    "people.csv",
    "id,name,amount,joined,zip\n" +
      '1,"Smith, Jo",12.50,2026-01-02,00123\n' +
      '2,"two\nlines",,2026-02-03,02134\n' +
      "3,,7,,90210\n",
  );
  const table = tableName(testInfo, "people");
  const dialog = await openUpload(page);
  await chooseFile(dialog, file, table);
  await expect(dialog.getByLabel("Delimiter")).toHaveAttribute("placeholder", "Detect (,)");
  await dialog.getByTestId("upload-preview-toggle").click();
  const preview = dialog.getByTestId("upload-preview");
  await preview.getByLabel("Type of column 3").selectOption("DECIMAL");
  await preview.getByLabel("Precision of column 3").fill("10");
  await preview.getByLabel("Scale of column 3").fill("2");
  await preview.getByLabel("Name of column 2").fill("full_name");
  await uploadAndWait(dialog);
  // The result lists the table's columns with their exact types.
  await expect(dialog.getByTestId("upload-data-result")).toContainText("DECIMAL(10,2)");
  expect(
    await rows(
      harness,
      `SELECT id, full_name, amount::VARCHAR AS amount, joined::VARCHAR AS joined, zip FROM main.${table} ORDER BY id`,
    ),
  ).toEqual([
    { id: 1n, full_name: "Smith, Jo", amount: "12.50", joined: "2026-01-02", zip: "00123" },
    { id: 2n, full_name: "two\nlines", amount: null, joined: "2026-02-03", zip: "02134" },
    { id: 3n, full_name: null, amount: "7.00", joined: null, zip: "90210" },
  ]);
  const types = await rows(
    harness,
    `SELECT data_type FROM information_schema.columns WHERE table_name = '${table}' ORDER BY ordinal_position`,
  );
  expect(types.map((t) => t["data_type"])).toEqual(["BIGINT", "VARCHAR", "DECIMAL(10,2)", "DATE", "VARCHAR"]);
});

test("Parquet and JSON lines keep their values", async ({ page, harness }, testInfo) => {
  const arrow = tableFromArrays({
    id: Int32Array.from([1, 2, 3]),
    name: ["a", null, "c"],
    score: Float64Array.from([0.5, 1.25, -2]),
  });
  const parquet = writeParquet(WasmTable.fromIPCStream(tableToIPC(arrow, "stream")));
  const pq = write(testInfo, "scores.parquet", parquet);
  const pqTable = tableName(testInfo, "scores");
  let dialog = await openUpload(page);
  await chooseFile(dialog, pq, pqTable);
  expect(await uploadAndWait(dialog)).toContain("(create, PARQUET,");
  expect(await rows(harness, `SELECT * FROM main.${pqTable} ORDER BY id`)).toEqual([
    { id: 1, name: "a", score: 0.5 },
    { id: 2, name: null, score: 1.25 },
    { id: 3, name: "c", score: -2 },
  ]);
  await dialog.getByRole("button", { name: "Done" }).click();

  const jsonl = write(
    testInfo,
    "events.jsonl",
    '{"id": 1, "kind": "open", "at": "2026-10-20T08:00:00Z", "meta": {"ok": true}}\n' +
      '{"id": 2, "kind": "close", "at": "2026-10-20T09:30:00Z"}\n',
  );
  const jsonTable = tableName(testInfo, "events");
  dialog = await openUpload(page);
  await chooseFile(dialog, jsonl, jsonTable);
  await uploadAndWait(dialog);
  expect(
    await rows(harness, `SELECT id, kind, epoch_ms("at") AS at_ms, meta FROM main.${jsonTable} ORDER BY id`),
  ).toEqual([
    { id: 1n, kind: "open", at_ms: BigInt(Date.UTC(2026, 9, 20, 8)), meta: '{"ok":true}' },
    { id: 2n, kind: "close", at_ms: BigInt(Date.UTC(2026, 9, 20, 9, 30)), meta: null },
  ]);
});

test("append matches columns by name; replace replaces the table; create refuses an existing one", async ({
  page,
  harness,
}, testInfo) => {
  const table = tableName(testInfo, "ledger");
  const first = write(testInfo, "ledger.csv", "id,item,qty\n1,apple,3\n2,pear,5\n");
  const dialog = await openUpload(page);
  await chooseFile(dialog, first, table);
  await uploadAndWait(dialog);

  // Append: the columns in another order, one missing (it gets NULL).
  await dialog.getByRole("button", { name: "Upload another" }).click();
  const more = write(testInfo, "more.csv", "item,id\nplum,3\n");
  await chooseFile(dialog, more, table);
  await dialog.getByLabel("Append to an existing table (columns matched by name)").check();
  await expect(dialog.getByTestId("upload-warning")).toContainText('The file has no "qty": those columns get NULL.');
  expect(await uploadAndWait(dialog)).toContain("(append, CSV,");
  expect(await rows(harness, `SELECT * FROM main.${table} ORDER BY id`)).toEqual([
    { id: 1n, item: "apple", qty: 3n },
    { id: 2n, item: "pear", qty: 5n },
    { id: 3n, item: "plum", qty: null },
  ]);

  // A column the table doesn't have stops an append before it starts.
  await dialog.getByRole("button", { name: "Upload another" }).click();
  const extra = write(testInfo, "extra.csv", "id,colour\n4,red\n");
  await chooseFile(dialog, extra, table);
  await dialog.getByLabel("Append to an existing table (columns matched by name)").check();
  await expect(dialog.getByTestId("upload-problem")).toContainText('the table has no column "colour"');
  await expect(dialog.getByRole("button", { name: "Upload", exact: true })).toBeDisabled();

  // Create refuses a table that exists.
  await dialog.getByLabel("Create a new table (fails if it exists)").check();
  await dialog.getByRole("button", { name: "Upload", exact: true }).click();
  await expect(dialog.getByTestId("upload-error")).toContainText(
    `Table ${harness.catalog}.main.${table} already exists — choose Append or Replace, or another name.`,
  );

  // Replace: the file's columns and rows, nothing of the old table.
  await dialog.getByLabel("Replace the table's contents").check();
  expect(await uploadAndWait(dialog)).toContain("(replace, CSV,");
  expect(await rows(harness, `SELECT * FROM main.${table}`)).toEqual([{ id: 4n, colour: "red" }]);
});

/** A CSV of about `mb` megabytes, written once per run. */
async function bigCsv(mb: number): Promise<{ file: string; rows: number }> {
  const file = path.join(os.tmpdir(), `hardcharger-ui-upload-${mb}mb-${process.pid}.csv`);
  const out = createWriteStream(file);
  let size = 0;
  let n = 0;
  out.write("id,customer,amount,ordered_on,note\n");
  while (size < mb * 1024 * 1024) {
    let block = "";
    for (let i = 0; i < 10_000; i++, n++) {
      block += `${n},customer ${n % 997},${((n * 37) % 100_000) / 100},2026-${String((n % 12) + 1).padStart(2, "0")}-${String((n % 28) + 1).padStart(2, "0")},"row ${n}, with a quoted comma"\n`;
    }
    size += block.length;
    if (!out.write(block)) await once(out, "drain");
  }
  out.end();
  await once(out, "finish");
  return { file, rows: n };
}

let big: Promise<{ file: string; rows: number }> | undefined;

test("a 200 MB CSV uploads in pieces of a few megabytes, with progress", async ({ page, harness }, testInfo) => {
  test.setTimeout(300_000);
  const { file, rows: count } = await (big ??= bigCsv(200));
  const table = tableName(testInfo, "big_csv");
  const ingests: Promise<number>[] = [];
  page.on("requestfinished", (req) => {
    if (req.method() === "POST" && /\/ingest(\?|$)/.test(req.url())) {
      ingests.push(req.sizes().then((s) => s.requestBodySize));
    }
  });
  const dialog = await openUpload(page);
  await chooseFile(dialog, file, table);
  await dialog.getByRole("button", { name: "Upload", exact: true }).click();
  const bar = dialog.getByRole("progressbar", { name: "Upload progress" });
  const seen = new Set<number>();
  let heapPeak = 0;
  const result = dialog.getByTestId("upload-data-result");
  while (!(await result.isVisible())) {
    const now = await bar.getAttribute("aria-valuenow").catch(() => null);
    if (now !== null) seen.add(Number(now));
    heapPeak = Math.max(
      heapPeak,
      await page.evaluate(() => (performance as { memory?: { usedJSHeapSize: number } }).memory?.usedJSHeapSize ?? 0),
    );
    await page.waitForTimeout(250);
  }
  await expect(result).toContainText(`Loaded ${count.toLocaleString("en-US")} rows`);
  // Progress moved through the file, not from 0 to done.
  expect([...seen].filter((v) => v > 0 && v < 100).length).toBeGreaterThan(3);
  // Many requests, each a few megabytes: the file was never sent (or held) whole.
  const sizes = await Promise.all(ingests);
  expect(sizes.length).toBeGreaterThan(20);
  expect(Math.max(...sizes)).toBeLessThan(16 * 1024 * 1024);
  // Chromium reports the page's heap: it stays far below the file's size.
  if (testInfo.project.name === "chromium") expect(heapPeak).toBeLessThan(200 * 1024 * 1024);
  testInfo.annotations.push({
    type: "upload",
    description: `${sizes.length} requests, largest ${Math.max(...sizes)} bytes, page heap peak ${heapPeak} bytes`,
  });
  const check = await rows(
    harness,
    `SELECT count(*) AS n, min(id) AS lo, max(id) AS hi, sum(amount)::VARCHAR AS total, ` +
      `any_value(note) FILTER (WHERE id = 12345) AS note FROM main.${table}`,
  );
  let total = 0;
  for (let n = 0; n < count; n++) total += (n * 37) % 100_000;
  expect(check[0]).toMatchObject({
    n: BigInt(count),
    lo: 0n,
    hi: BigInt(count - 1),
    note: "row 12345, with a quoted comma",
  });
  expect(Number(check[0]!["total"])).toBeCloseTo(total / 100, 0);
});

test("cancelling an upload leaves no table behind", async ({ page, harness }, testInfo) => {
  test.setTimeout(300_000);
  const { file } = await (big ??= bigCsv(200));
  const table = tableName(testInfo, "cancelled");
  const dialog = await openUpload(page);
  await chooseFile(dialog, file, table);
  await dialog.getByRole("button", { name: "Upload", exact: true }).click();
  const bar = dialog.getByRole("progressbar", { name: "Upload progress" });
  await expect
    .poll(async () => Number(await bar.getAttribute("aria-valuenow")), { timeout: 60_000 })
    .toBeGreaterThan(5);
  // The tables's rows so far are in the upload's transaction, not visible to anyone else.
  expect(await tableExists(harness, harness.catalog, table)).toBe(false);
  await dialog.getByRole("button", { name: "Cancel upload" }).click();
  await expect(dialog.getByTestId("upload-error")).toHaveText("Upload cancelled.");
  await expect(dialog.getByRole("button", { name: "Upload", exact: true })).toBeEnabled();
  expect(await tableExists(harness, harness.catalog, table)).toBe(false);
  // And the server isn't still running it: no other session is busy.
  await expect
    .poll(async () => {
      const busy = await rows(
        harness,
        "SELECT count(*) AS n FROM hardcharger_sessions() WHERE running AND NOT is_current",
      );
      return busy[0]!["n"];
    })
    .toBe(0n);
});

test("a user without CREATE gets the server's permission error, and nothing is created", async ({
  browser,
  harness,
}) => {
  const context = await browser.newContext({ baseURL: harness.url });
  const page = await context.newPage();
  const check = await watchPage(page);
  await signIn(page, harness.user);
  await editorFocused(page);
  expectProblem(page, { status: 403, url: /\/ingest(\?|$)/ });
  const file = path.join(os.tmpdir(), `hardcharger-ui-upload-denied-${process.pid}.csv`);
  writeFileSync(file, "id,name\n1,a\n");
  const dialog = await openUpload(page);
  await chooseFile(dialog, file, "not_allowed");
  await dialog.getByRole("button", { name: "Upload", exact: true }).click();
  await expect(dialog.getByTestId("upload-error")).toContainText("Permission denied:");
  await expect(dialog.getByTestId("upload-error")).toContainText(/CREATE|create/);
  expect(await tableExists(harness, harness.user.catalog, "not_allowed")).toBe(false);
  check();
  await context.close();
});

test("a file dropped on the page, and the schema browser's Upload data…", async ({ page, harness }) => {
  const dt = await page.evaluateHandle(() => {
    const d = new DataTransfer();
    d.items.add(new File(["a,b\n1,2\n"], "Dropped File.csv", { type: "text/csv" }));
    return d;
  });
  await page.dispatchEvent("main", "dragenter", { dataTransfer: dt });
  await expect(page.getByTestId("upload-drop-overlay")).toBeVisible();
  await page.dispatchEvent("main", "drop", { dataTransfer: dt });
  await expect(page.getByTestId("upload-drop-overlay")).toHaveCount(0);
  const dialog = page.getByTestId("upload-data-dialog");
  await expect(dialog.getByTestId("upload-file-note")).toContainText("Dropped File.csv — 8 B");
  await expect(dialog.getByLabel("Table", { exact: true })).toHaveValue("dropped_file");
  await expect(dialog.getByTestId("upload-preview-toggle")).toContainText("2 of 2 columns");
  await dialog.getByRole("button", { name: "Close", exact: true }).last().click();
  await expect(dialog).toHaveCount(0);

  // The schema browser: a schema's menu opens the dialog on that catalog and schema.
  const catalog = page.getByRole("treeitem", { name: harness.catalog, exact: true });
  const sales = catalog.getByRole("treeitem", { name: "sales", exact: true });
  await sales.locator(":scope > .tree-row-wrap").click({ button: "right" });
  await page.getByRole("menuitem", { name: "Upload data…" }).click();
  await expect(dialog.getByLabel("Catalog")).toHaveValue(harness.catalog);
  await expect(dialog.getByLabel("Schema")).toHaveValue("sales");
  await expect(dialog.getByTestId("upload-target")).toHaveText(`→ ${harness.catalog}.sales.…`);
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await page.getByTestId("explorer-upload").click();
  await expect(dialog).toBeVisible();
});

test("screenshots of the upload dialog, light and dark", async ({ page }, testInfo) => {
  test.setTimeout(300_000);
  await page.setViewportSize({ width: 1360, height: 1000 });
  const shot = async (name: string) => {
    for (const theme of ["light", "dark"]) {
      await page.evaluate((t) => (document.documentElement.dataset["theme"] = t), theme);
      await page.screenshot({ path: testInfo.outputPath(`${name}-${theme}.png`) });
    }
    await page.evaluate(() => (document.documentElement.dataset["theme"] = "light"));
  };
  const book = await buildXlsx([
    {
      name: "Q3",
      rows: [
        [{ s: "region" }, { s: "rep" }, { s: "closed" }, { s: "amount" }, { s: "won" }],
        [
          { s: "North" },
          { s: "Ana Lima" },
          { n: serial(2026, 7, 3), style: "date" },
          { n: 18250, style: "money" },
          { b: true },
        ],
        [
          { s: "South" },
          { s: "Raj Patel" },
          { n: serial(2026, 8, 14), style: "date" },
          { n: 9420.5, style: "money" },
          { b: false },
        ],
        [
          { s: "East" },
          { s: "Mei Chen" },
          { n: serial(2026, 9, 29), style: "date" },
          { n: 30110, style: "money" },
          { b: true },
        ],
      ],
    },
    {
      name: "Targets",
      rows: [
        [{ s: "region" }, { s: "target" }],
        [{ s: "North" }, { n: 20000 }],
      ],
    },
  ]);
  const file = write(testInfo, "Sales Q3 2026.xlsx", book);
  await shot("upload-toolbar");
  const dialog = await openUpload(page);
  await chooseFile(dialog, file, "sales_q3_2026");
  await shot("upload-dialog");
  await dialog.getByTestId("upload-preview-toggle").click();
  await dialog.getByTestId("upload-preview").getByLabel("Type of column 4").selectOption("DECIMAL");
  await dialog.getByTestId("upload-preview").scrollIntoViewIfNeeded();
  await shot("upload-preview");
  // Each browser's run loads the same table: replace it.
  await dialog.getByLabel("Replace the table's contents").check();
  await uploadAndWait(dialog);
  await shot("upload-result");
  await dialog.getByRole("button", { name: "Upload another" }).click();
  const { file: bigFile } = await (big ??= bigCsv(200));
  await chooseFile(dialog, bigFile, "orders_2026");
  await dialog.getByTestId("upload-preview-toggle").click();
  await dialog.getByRole("button", { name: "Upload", exact: true }).click();
  const bar = dialog.getByRole("progressbar", { name: "Upload progress" });
  await expect
    .poll(async () => Number(await bar.getAttribute("aria-valuenow")), { timeout: 60_000 })
    .toBeGreaterThan(20);
  await shot("upload-progress");
  await dialog.getByRole("button", { name: "Cancel upload" }).click();
  await expect(dialog.getByTestId("upload-error")).toHaveText("Upload cancelled.");
});
