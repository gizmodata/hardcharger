import { readFileSync } from "node:fs";
import { tableFromIPC } from "apache-arrow";
import { readParquet } from "parquet-wasm";
import { activeTab, expect, gridRows, runDone, signIn, test } from "./fixtures.ts";

test("VARIANT values are decoded with logical types; ordinary structs keep their shape", async ({ page, harness }) => {
  await signIn(page, harness.admin);
  await runDone(
    page,
    "SELECT 42::VARIANT AS v, {'a': 1, 'b': [true, NULL, false]}::VARIANT AS obj, [42::VARIANT, NULL::VARIANT, 'hi'::VARIANT]::VARIANT AS arr, NULL::VARIANT AS n, {'metadata': 'a'::BLOB, 'value': 'b'::BLOB} AS ordinary",
  );
  const rows = await gridRows(page);
  expect(rows[1]).toEqual([
    "42",
    '{"a":1,"b":[true,null,false]}',
    '[42,null,"hi"]',
    "NULL",
    "{'metadata': a, 'value': b}",
  ]);
  await expect(page.locator(".grid-head-type").first()).toHaveText("VARIANT");
  for (const label of ["JSON", "Arrow IPC (.arrows)", "Parquet"]) {
    await activeTab(page).getByTestId("export-button").click();
    const download = page.waitForEvent("download");
    await page.getByRole("menuitem", { name: label, exact: true }).click();
    const bytes = readFileSync(await (await download).path());
    if (label === "JSON") {
      expect(JSON.parse(bytes.toString())[0]).toMatchObject({
        v: 42,
        obj: { a: 1, b: [true, null, false] },
        arr: [42, null, "hi"],
        n: null,
      });
    } else {
      const table = tableFromIPC(label === "Parquet" ? readParquet(new Uint8Array(bytes)).intoIPCStream() : bytes);
      expect(table.schema.fields[0]!.metadata.get("ARROW:extension:name")).toBe("arrow.parquet.variant");
      expect(table.getChildAt(0)!.get(0).value).toBeInstanceOf(Uint8Array);
    }
  }
  await runDone(page, "SELECT name, typed_value FROM duckdb_settings() WHERE name = 'threads'");
  await expect.poll(async () => (await gridRows(page))[1]?.[1]).toMatch(/^\d+$/);
});
