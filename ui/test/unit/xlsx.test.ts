import { inflateRawSync } from "node:zlib";

import { tableFromArrays } from "apache-arrow";
import { describe, expect, it } from "vitest";

import { columnName, tableToXlsx, xmlText } from "../../src/lib/xlsx";
import { crc32, zip } from "../../src/lib/zip";

/** The files of a zip, read back (stored or deflated), with their CRCs checked. */
function unzip(bytes: Uint8Array): Map<string, string> {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const out = new Map<string, string>();
  let at = 0;
  while (view.getUint32(at, true) === 0x04034b50) {
    const method = view.getUint16(at + 8, true);
    const crc = view.getUint32(at + 14, true);
    const size = view.getUint32(at + 18, true);
    const nameLength = view.getUint16(at + 26, true);
    const name = new TextDecoder().decode(bytes.subarray(at + 30, at + 30 + nameLength));
    const body = bytes.subarray(at + 30 + nameLength, at + 30 + nameLength + size);
    const data = method === 8 ? new Uint8Array(inflateRawSync(body)) : body;
    expect(crc32(data), name).toBe(crc);
    out.set(name, new TextDecoder().decode(data));
    at += 30 + nameLength + size;
  }
  return out;
}

describe("zip", () => {
  it("stores or deflates entries that read back exactly, UTF-8 names included", async () => {
    const files = [
      { path: "a/ü.sql", data: new TextEncoder().encode("SELECT 1;") },
      { path: "big.txt", data: new TextEncoder().encode("x".repeat(10_000)) },
    ];
    for (const compress of [true, false]) {
      const read = unzip(await zip(files, { compress }));
      expect([...read.keys()]).toEqual(["a/ü.sql", "big.txt"]);
      expect(read.get("big.txt")).toHaveLength(10_000);
    }
    expect(crc32(new TextEncoder().encode("123456789"))).toBe(0xcbf43926);
  });
});

describe("xlsx", () => {
  it("column letters and XML text", () => {
    expect([0, 25, 26, 701, 702].map(columnName)).toEqual(["A", "Z", "AA", "ZZ", "AAA"]);
    expect(xmlText('a<b>&"\u0001c')).toBe("a&lt;b&gt;&amp;&quot;c");
  });

  it("a sheet: header, numbers as numbers, text as text (leading zeros kept), NULL empty, rows in the shown order", async () => {
    const table = tableFromArrays({
      id: Int32Array.from([1, 2, 3]),
      code: ["00123", "x<y", null],
      big: BigInt64Array.from([1n, 9007199254740993n, -5n]),
      ok: [true, false, null],
    });
    const files = unzip(await tableToXlsx(table, { order: [2, 0] }));
    expect([...files.keys()].sort()).toEqual([
      "[Content_Types].xml",
      "_rels/.rels",
      "xl/_rels/workbook.xml.rels",
      "xl/styles.xml",
      "xl/workbook.xml",
      "xl/worksheets/sheet1.xml",
    ]);
    const sheet = files.get("xl/worksheets/sheet1.xml")!;
    const rows = [...sheet.matchAll(/<row r="(\d+)">(.*?)<\/row>/g)].map((m) => m[2]);
    expect(rows).toHaveLength(3);
    expect(rows[0]).toContain('<c r="A1" s="1" t="inlineStr"><is><t xml:space="preserve">id</t></is></c>');
    // Row 3 of the table first: its code is NULL (no cell), its bool NULL.
    expect(rows[1]).toBe('<c r="A2"><v>3</v></c><c r="C2"><v>-5</v></c>');
    expect(rows[2]).toBe(
      '<c r="A3"><v>1</v></c><c r="B3" t="inlineStr"><is><t xml:space="preserve">00123</t></is></c><c r="C3"><v>1</v></c><c r="D3" t="b"><v>1</v></c>',
    );
    // An integer beyond 2^53 is text, so no digit is lost.
    const all = unzip(await tableToXlsx(table));
    expect(all.get("xl/worksheets/sheet1.xml")).toContain('<t xml:space="preserve">9007199254740993</t>');
    expect(all.get("xl/worksheets/sheet1.xml")).toContain("x&lt;y");
  });
});
