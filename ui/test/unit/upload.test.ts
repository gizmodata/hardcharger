// @vitest-environment node
// (Node's File and Blob stream; the converters run in a worker in the browser, without the DOM.)

import { DataType, type Table, tableFromIPC, tableToIPC, Table as ArrowTable } from "apache-arrow";
import { describe, expect, it } from "vitest";
import * as XLSX from "xlsx";

import { CsvParser, parseCsv, sniffDelimiter, typedDelimiter } from "../../src/lib/upload/csv";
import { formatShows, sheetNames } from "../../src/lib/upload/excel";
import { defaultTableName, formatFromBytes, formatFromName, nameProblem } from "../../src/lib/upload/formats";
import { columnNames, detectHeader, inferType } from "../../src/lib/upload/infer";
import { parseJson } from "../../src/lib/upload/json";
import { type Inspection, type ReadOptions, UploadError, convert, inspect } from "../../src/lib/upload/sources";
import { type Cell, type ColumnType, type OutputColumn, columnTypeOf } from "../../src/lib/upload/types";
import {
  ValueError,
  converter,
  dateText,
  momentText,
  parseDecimal,
  parseTimestamp,
  serialToMs,
  timestampText,
  valueText,
} from "../../src/lib/upload/values";
import { buildXlsx, serial } from "./workbook";

const auto: ReadOptions = { header: "auto", delimiter: "", sheet: "" };

function fileOf(content: string | Uint8Array, name: string): File {
  return new File([content as BlobPart], name);
}

/** Every batch of an upload, as one table read back from Arrow IPC (as the server reads it). */
async function upload(file: File, inspection: Inspection, columns?: OutputColumn[]): Promise<Table> {
  const plan =
    columns ?? inspection.columns.map((c, i) => ({ name: c.name, type: c.inferred, source: i }) satisfies OutputColumn);
  const batches = [];
  for await (const chunk of convert(file, inspection.settings, plan, inspection.context)) {
    // Every fixed-width column has room for every row, trailing NULLs included (the server's
    // Arrow reader refuses a buffer that is too small; Arrow JS reads past it silently).
    for (const child of chunk.batch.data.children) {
      const width = (child.type as { ArrayType?: { BYTES_PER_ELEMENT: number } }).ArrayType?.BYTES_PER_ELEMENT;
      const stride = (child as unknown as { stride: number }).stride;
      if (child.values && width && !child.valueOffsets && !DataType.isBool(child.type)) {
        expect(child.values.length, `type id ${child.typeId}`).toBeGreaterThanOrEqual(child.length * stride);
      }
    }
    batches.push(chunk.batch);
  }
  return tableFromIPC(tableToIPC(new ArrowTable(batches), "stream"));
}

const rowsOf = (t: Table) => t.toArray().map((r) => (r as { toJSON: () => Record<string, unknown> }).toJSON());
const types = (i: Inspection) => i.columns.map((c) => c.inferred.kind);

describe("values", () => {
  it("rounds decimals half away from zero and refuses what doesn't fit", () => {
    expect(parseDecimal("1.005", 10, 2)).toBe(101n);
    expect(parseDecimal("-1.005", 10, 2)).toBe(-101n);
    expect(parseDecimal("1.2e3", 10, 2)).toBe(120000n);
    expect(parseDecimal(".5", 3, 1)).toBe(5n);
    expect(parseDecimal("12345678.9", 12, 2)).toBe(1234567890n);
    expect(() => parseDecimal("123", 4, 2)).toThrow(ValueError);
    expect(() => parseDecimal("1,5", 4, 2)).toThrow(ValueError);
    expect(valueText({ kind: "DECIMAL", precision: 10, scale: 2 }, -5n)).toBe("-0.05");
  });

  it("reads timestamps with and without an offset, in UTC", () => {
    expect(timestampText(parseTimestamp("2026-10-20 10:30:00").micros)).toBe("2026-10-20 10:30:00");
    expect(timestampText(parseTimestamp("2026-10-20T10:30:00.123456").micros)).toBe("2026-10-20 10:30:00.123456");
    const zoned = parseTimestamp("2026-10-20T10:30:00+02:00");
    expect(zoned.zoned).toBe(true);
    expect(timestampText(zoned.micros)).toBe("2026-10-20 08:30:00");
    expect(timestampText(parseTimestamp("2026-10-20 10:30Z").micros)).toBe("2026-10-20 10:30:00");
    expect(() => parseTimestamp("2026-02-30 10:00")).toThrow(ValueError);
    expect(timestampText(parseTimestamp("1969-12-31 23:59:59").micros)).toBe("1969-12-31 23:59:59");
  });

  it("turns Excel serial dates into dates, in both date systems", () => {
    const day = (ms: number) => dateText(Math.floor(ms / 86_400_000));
    expect(day(serialToMs(1))).toBe("1900-01-01");
    expect(day(serialToMs(59))).toBe("1900-02-28");
    expect(() => serialToMs(60)).toThrow(ValueError); // Lotus's 1900-02-29
    expect(day(serialToMs(61))).toBe("1900-03-01");
    expect(day(serialToMs(45950))).toBe("2025-10-20");
    expect(day(serialToMs(0, true))).toBe("1904-01-01");
    expect(day(serialToMs(45950 - 1462, true))).toBe("2025-10-20");
    expect(momentText({ ms: serialToMs(45950.75), shows: "datetime" })).toBe("2025-10-20 18:00:00");
    expect(momentText({ ms: serialToMs(0.5), shows: "time" })).toBe("12:00:00");
  });

  it("converts cells to each type, and says why one doesn't fit", () => {
    const big = converter({ kind: "BIGINT" });
    expect(big("42")).toBe(42n);
    expect(big(7)).toBe(7n);
    expect(() => big("4.5")).toThrow("not a whole number");
    expect(() => big(1.5)).toThrow(ValueError);
    expect(converter({ kind: "BOOLEAN" })("Yes")).toBe(true);
    expect(converter({ kind: "DATE" })("2026-01-31")).toBe(20484);
    expect(() => converter({ kind: "DATE" })(45950)).toThrow(ValueError); // a number isn't a date outside Excel
    expect(converter({ kind: "DATE" }, { serialDates: true })(45950)).toBe(20381);
    expect(converter({ kind: "VARCHAR" })(1.5)).toBe("1.5");
    expect(converter({ kind: "VARCHAR" })(true)).toBe("true");
    expect(converter({ kind: "TIME" })("23:59:59.5")).toBe(86_399_500_000n);
    expect(converter({ kind: "DOUBLE" })("-inf")).toBe(-Infinity);
  });

  it("maps the server's column types to the upload's", () => {
    expect(columnTypeOf("DECIMAL(12,2)")).toEqual({ kind: "DECIMAL", precision: 12, scale: 2 });
    expect(columnTypeOf("INTEGER")).toEqual({ kind: "BIGINT" });
    expect(columnTypeOf("TIMESTAMP WITH TIME ZONE")).toEqual({ kind: "TIMESTAMPTZ" });
    expect(columnTypeOf("VARCHAR")).toEqual({ kind: "VARCHAR" });
    expect(columnTypeOf("INTEGER[]")).toBeUndefined();
  });
});

describe("type inference", () => {
  const infer = (values: Cell[]) => inferType(values, "full").kind;
  it("infers from text as DuckDB's sniffer does, keeping leading zeros as text", () => {
    expect(infer(["1", "2", null, "-3"])).toBe("BIGINT");
    expect(infer(["1", "2.5"])).toBe("DOUBLE");
    expect(infer(["00123", "00456"])).toBe("VARCHAR");
    expect(infer(["1", "x"])).toBe("VARCHAR");
    expect(infer(["true", "FALSE"])).toBe("BOOLEAN");
    expect(infer(["2026-01-01", "2026-12-31"])).toBe("DATE");
    expect(infer(["2026-01-01", "2026-12-31 10:00"])).toBe("TIMESTAMP");
    expect(infer(["2026-01-01T10:00:00Z"])).toBe("TIMESTAMPTZ");
    expect(infer(["10:00", "23:59:59"])).toBe("TIME");
    expect(infer([null, ""])).toBe("VARCHAR");
    expect(infer(["99999999999999999999"])).toBe("DOUBLE");
  });

  it("keeps a spreadsheet's text as text, and its numbers and dates as theirs", () => {
    expect(inferType(["00123", "42"], "none").kind).toBe("VARCHAR");
    expect(inferType([1, 2, 3], "none").kind).toBe("BIGINT");
    expect(inferType([1, 2.5], "none").kind).toBe("DOUBLE");
    expect(inferType([true, false], "none").kind).toBe("BOOLEAN");
    expect(
      inferType(
        [
          { ms: 0, shows: "date" },
          { ms: 86_400_000, shows: "date" },
        ],
        "none",
      ).kind,
    ).toBe("DATE");
    expect(
      inferType(
        [
          { ms: 0, shows: "date" },
          { ms: 1000, shows: "datetime" },
        ],
        "none",
      ).kind,
    ).toBe("TIMESTAMP");
    expect(inferType([1, "a"], "none").kind).toBe("VARCHAR");
  });

  it("detects a header row, and names columns as DuckDB does", () => {
    expect(
      detectHeader(
        [
          ["id", "name"],
          ["1", "a"],
        ],
        2,
        "full",
      ),
    ).toBe(true);
    expect(
      detectHeader(
        [
          ["1", "a"],
          ["2", "b"],
        ],
        2,
        "full",
      ),
    ).toBe(false);
    expect(
      detectHeader(
        [
          ["city", "country"],
          ["Paris", "France"],
        ],
        2,
        "full",
      ),
    ).toBe(true);
    expect(
      detectHeader(
        [
          ["a", "a"],
          ["1", "2"],
        ],
        2,
        "full",
      ),
    ).toBe(false);
    expect(columnNames(["id", "", "ID", " name "], 4)).toEqual(["id", "column1", "ID_1", "name"]);
    expect(columnNames(undefined, 2)).toEqual(["column0", "column1"]);
  });

  it("derives a table name from the file name as GizmoData Cloud does", () => {
    expect(defaultTableName("Sales Q3 2026.xlsx")).toBe("sales_q3_2026");
    expect(defaultTableName("2026 report.csv")).toBe("t_2026_report");
    expect(defaultTableName("___.csv")).toBe("uploaded_data");
    expect(nameProblem("", "table")).toBe("A table name is required.");
    expect(nameProblem("x".repeat(129), "table")).toMatch(/1–128 characters/);
  });

  it("tells formats by name, then by their first bytes", () => {
    expect(formatFromName("a.TSV")).toBe("tsv");
    expect(formatFromName("a.xlsb")).toBe("xlsb");
    expect(formatFromName("a.dat")).toBeUndefined();
    expect(formatFromBytes(new TextEncoder().encode("PAR1...."))).toBe("parquet");
    expect(formatFromBytes(new TextEncoder().encode("  [{}]"))).toBe("json");
    expect(formatFromBytes(new Uint8Array([0x50, 0x4b, 3, 4]))).toBe("xlsx");
    expect(formatFromBytes(new TextEncoder().encode("a,b\n1,2"))).toBe("csv");
  });
});

describe("CSV", () => {
  it("keeps quoted delimiters, quotes and line breaks; an unquoted empty field is NULL", () => {
    const { records, lines } = parseCsv('a,b,c\r\n"x, y","say ""hi""","line\nbreak"\n,"",z\n\n', ",");
    expect(records).toEqual([
      ["a", "b", "c"],
      ["x, y", 'say "hi"', "line\nbreak"],
      [null, "", "z"],
    ]);
    expect(lines).toEqual([1, 2, 4]);
  });

  it("reads the same records whatever size the chunks are", () => {
    const text = 'id;note\r\n1;"semi;colon"\r\n2;"multi\r\nline ""q"""\r\n3;last';
    const want = parseCsv(text, ";").records;
    for (const size of [1, 2, 3, 7]) {
      const got: (string | null)[][] = [];
      const p = new CsvParser(";", (f) => got.push(f));
      for (let i = 0; i < text.length; i += size) p.push(text.slice(i, i + size));
      p.end();
      expect(got, `chunks of ${size}`).toEqual(want);
    }
  });

  it("takes delimiters of several characters", () => {
    expect(parseCsv("a||b||c\n1||2||3", "||").records).toEqual([
      ["a", "b", "c"],
      ["1", "2", "3"],
    ]);
    expect(parseCsv("a|b||c", "||").records).toEqual([["a|b", "c"]]);
  });

  it("says where a quote is left open", () => {
    expect(() => parseCsv('a,b\n1,"open\n2,3\n', ",")).toThrow(/quote opened on line 2 is never closed/);
  });

  it("sniffs the delimiter", () => {
    expect(sniffDelimiter("a,b,c\n1,2,3\n4,5,6\n")).toBe(",");
    expect(sniffDelimiter("a;b;c\n1,5;2;3\n4;5;6\n")).toBe(";");
    expect(sniffDelimiter("a\tb\n1\t2\n")).toBe("\t");
    expect(sniffDelimiter('a|b\n"x|y"|2\n')).toBe("|");
    expect(typedDelimiter("\\t")).toBe("\t");
    expect(typedDelimiter("")).toBeUndefined();
  });

  it("uploads a CSV with mixed types, empty fields and quoted line breaks", async () => {
    const csv =
      "id,name,amount,joined,active,zip\n" +
      '1,"Smith, Jo",12.50,2026-01-02,true,00123\n' +
      '2,"two\nlines",,2026-02-03,false,02134\n' +
      "3,,7,,true,90210\n";
    const file = fileOf(csv, "people.csv");
    const found = await inspect(file, auto);
    expect(found.settings.delimiter).toBe(",");
    expect(found.settings.header).toBe(true);
    expect(found.columns.map((c) => c.name)).toEqual(["id", "name", "amount", "joined", "active", "zip"]);
    expect(types(found)).toEqual(["BIGINT", "VARCHAR", "DOUBLE", "DATE", "BOOLEAN", "VARCHAR"]);
    const t = await upload(file, found);
    expect(t.schema.fields.map((f) => String(f.type))).toEqual([
      "Int64",
      "Utf8",
      "Float64",
      "Date32<DAY>",
      "Bool",
      "Utf8",
    ]);
    expect(rowsOf(t)).toEqual([
      { id: 1n, name: "Smith, Jo", amount: 12.5, joined: Date.UTC(2026, 0, 2), active: true, zip: "00123" },
      { id: 2n, name: "two\nlines", amount: null, joined: Date.UTC(2026, 1, 3), active: false, zip: "02134" },
      { id: 3n, name: null, amount: 7, joined: null, active: true, zip: "90210" },
    ]);
  });

  it("names the line and column of a value that doesn't fit the chosen type", async () => {
    const file = fileOf("n\n1\n2\nthree\n", "n.csv");
    const found = await inspect(file, auto);
    const plan: OutputColumn[] = [{ name: "n", type: { kind: "BIGINT" }, source: 0 }];
    await expect(upload(file, found, plan)).rejects.toThrow(
      /^Line 4, column "n": "three" is not a whole number\. Change the column's type/,
    );
  });

  it("refuses a line with the wrong number of fields", async () => {
    const file = fileOf("a,b\n1,2\n3,4,5\n", "bad.csv");
    await expect(inspect(file, auto)).rejects.toThrow(/^Line 3 has 3 fields, but the file's first line has 2/);
  });

  it("reads a file that isn't UTF-8 as Windows-1252, and says so", async () => {
    const bytes = new Uint8Array([...new TextEncoder().encode("name\nJos"), 0xe9, 0x0a]);
    const file = fileOf(bytes, "latin.csv");
    const found = await inspect(file, auto);
    expect(found.notes.join(" ")).toMatch(/isn't UTF-8: it was read as Windows-1252/);
    expect(rowsOf(await upload(file, found))).toEqual([{ name: "José" }]);
  });

  it("splits a large file into batches of a few megabytes", async () => {
    const lines = ["id,text"];
    for (let i = 0; i < 300_000; i++) lines.push(`${i},row number ${i} with some padding text`);
    const file = fileOf(`${lines.join("\n")}\n`, "big.csv");
    const found = await inspect(file, auto);
    const chunks = [];
    for await (const c of convert(
      file,
      found.settings,
      found.columns.map((c, i) => ({ name: c.name, type: c.inferred, source: i })),
      {},
    )) {
      chunks.push(c);
    }
    expect(chunks.length).toBeGreaterThan(1);
    expect(chunks.reduce((n, c) => n + c.batch.numRows, 0)).toBe(300_000);
    expect(chunks.at(-1)!.fraction).toBe(1);
    for (let i = 1; i < chunks.length; i++) expect(chunks[i]!.fraction).toBeGreaterThanOrEqual(chunks[i - 1]!.fraction);
  });
});

describe("JSON", () => {
  it("reads JSON lines: columns in first-seen order, nested values as JSON text, dates detected", async () => {
    const text =
      '{"id": 1, "name": "a", "at": "2026-01-02", "tags": ["x", "y"]}\n' +
      '{"id": 2, "extra": {"k": true}, "at": null}\n\n' +
      '{"id": 3, "name": "c", "at": "2026-03-04"}\n';
    const file = fileOf(text, "events.ndjson");
    const found = await inspect(file, auto);
    expect(found.columns.map((c) => c.name)).toEqual(["id", "name", "at", "tags", "extra"]);
    expect(types(found)).toEqual(["BIGINT", "VARCHAR", "DATE", "VARCHAR", "VARCHAR"]);
    expect(rowsOf(await upload(file, found))).toEqual([
      { id: 1n, name: "a", at: Date.UTC(2026, 0, 2), tags: '["x","y"]', extra: null },
      { id: 2n, name: null, at: null, tags: null, extra: '{"k":true}' },
      { id: 3n, name: "c", at: Date.UTC(2026, 2, 4), tags: null, extra: null },
    ]);
  });

  it("reads a JSON array, and a single pretty-printed object", async () => {
    const arr = fileOf('[{"a": 1.5}, {"a": 2}]', "a.json");
    const found = await inspect(arr, auto);
    expect(found.settings.jsonArray).toBe(true);
    expect(rowsOf(await upload(arr, found))).toEqual([{ a: 1.5 }, { a: 2 }]);
    const obj = fileOf('{\n  "a": "x",\n  "b": 2\n}\n', "one.json");
    const one = await inspect(obj, auto);
    expect(rowsOf(await upload(obj, one))).toEqual([{ a: "x", b: 2n }]);
  });

  it("refuses a row that isn't an object, naming its line", async () => {
    const file = fileOf('{"a": 1}\n[1, 2]\n', "bad.jsonl");
    await expect(inspect(file, auto)).rejects.toThrow(/^Line 2 is not a JSON object/);
  });

  it("keeps a big integer's digits where JSON.parse shows its source", () => {
    const value = parseJson('{"n": 12345678901234567890}') as { n: unknown };
    // Node 24 has JSON.parse source text access.
    expect(value.n).toBe("12345678901234567890");
  });
});

describe("Excel", () => {
  const sales = {
    name: "Sales",
    rows: [
      [{ s: "id" }, { s: "customer" }, { s: "amount" }, { s: "ordered" }, { s: "paid" }, { s: "code" }, { s: "at" }],
      [
        { n: 1 },
        { s: "Acme" },
        { n: 1234.5, style: "money" as const },
        { n: serial(2026, 1, 31), style: "date" as const },
        { b: true },
        { s: "00123" },
        { n: serial(2026, 1, 31, 9, 30), style: "datetime" as const },
      ],
      null,
      [
        { n: 2 },
        { is: "Beta & Co" },
        { f: "A4*10", v: 20 },
        { n: serial(2024, 2, 29), style: "isoDate" as const },
        { b: false },
        { s: "042" },
        null,
      ],
      [
        { n: 3 },
        null,
        { e: "#N/A" },
        null,
        null,
        { s: "7" },
        { n: serial(2026, 12, 31, 23, 59, 59), style: "datetime" as const },
      ],
    ],
  };
  const second = {
    name: "Notes",
    rows: [
      [{ s: "note" }, { s: "when" }],
      [{ s: "hello" }, { n: 0.75, style: "time" as const }],
    ],
  };

  it("lists sheets and reads the first one: dates, numbers, booleans, text, blank cells", async () => {
    const bytes = await buildXlsx([sales, second]);
    expect(await sheetNames(bytes)).toEqual(["Sales", "Notes"]);
    const file = fileOf(bytes, "Sales Q3 2026.xlsx");
    const found = await inspect(file, auto);
    expect(found.sheets).toEqual(["Sales", "Notes"]);
    expect(found.settings.sheet).toBe("Sales");
    expect(found.settings.header).toBe(true);
    expect(found.rows).toBe(3);
    expect(found.columns.map((c) => c.name)).toEqual(["id", "customer", "amount", "ordered", "paid", "code", "at"]);
    expect(types(found)).toEqual(["BIGINT", "VARCHAR", "DOUBLE", "DATE", "BOOLEAN", "VARCHAR", "TIMESTAMP"]);
    expect(found.notes).toEqual(
      expect.arrayContaining([expect.stringMatching(/^1 empty row left out/), expect.stringMatching(/^1 error cell/)]),
    );
    expect(rowsOf(await upload(file, found))).toEqual([
      {
        id: 1n,
        customer: "Acme",
        amount: 1234.5,
        ordered: Date.UTC(2026, 0, 31),
        paid: true,
        code: "00123",
        at: Date.UTC(2026, 0, 31, 9, 30),
      },
      { id: 2n, customer: "Beta & Co", amount: 20, ordered: Date.UTC(2024, 1, 29), paid: false, code: "042", at: null },
      {
        id: 3n,
        customer: null,
        amount: null,
        ordered: null,
        paid: null,
        code: "7",
        at: Date.UTC(2026, 11, 31, 23, 59, 59),
      },
    ]);
  });

  it("reads another sheet, and a time of day", async () => {
    const file = fileOf(await buildXlsx([sales, second]), "book.xlsx");
    const found = await inspect(file, { ...auto, sheet: "Notes" });
    expect(types(found)).toEqual(["VARCHAR", "TIME"]);
    expect(rowsOf(await upload(file, found))).toEqual([{ note: "hello", when: 18n * 3600n * 1_000_000n }]);
  });

  it("reads the 1904 date system", async () => {
    const book = {
      name: "Mac",
      rows: [[{ s: "day" }], [{ n: serial(2026, 10, 20, 0, 0, 0, true), style: "date" as const }]],
    };
    const file = fileOf(await buildXlsx([book], { date1904: true }), "mac.xlsx");
    const found = await inspect(file, auto);
    expect(found.context.epoch1904).toBe(true);
    expect(rowsOf(await upload(file, found))).toEqual([{ day: Date.UTC(2026, 9, 20) }]);
  });

  it("converts numbers stored as text when asked, and names the cell that doesn't convert", async () => {
    const book = { name: "S", rows: [[{ s: "code" }], [{ s: "00123" }], [{ s: "42" }], [{ s: "n/a" }]] };
    const file = fileOf(await buildXlsx([book]), "codes.xlsx");
    const found = await inspect(file, auto);
    expect(types(found)).toEqual(["VARCHAR"]);
    const asInt: OutputColumn[] = [{ name: "code", type: { kind: "BIGINT" }, source: 0 }];
    await expect(upload(file, found, asInt)).rejects.toThrow(/^Cell A4, column "code": "n\/a" is not a whole number/);
    const two = { name: "S", rows: [[{ s: "code" }], [{ s: "00123" }], [{ s: "42" }]] };
    const file2 = fileOf(await buildXlsx([two]), "codes.xlsx");
    expect(rowsOf(await upload(file2, await inspect(file2, auto), asInt))).toEqual([{ code: 123n }, { code: 42n }]);
  });

  it("keeps a merged range's value in its first cell, and says so", async () => {
    const book = {
      name: "M",
      rows: [
        [{ s: "region" }, { s: "q" }],
        [{ s: "North" }, { n: 1 }],
        [null, { n: 2 }],
      ],
      merges: ["A2:A3"],
    };
    const file = fileOf(await buildXlsx([book]), "merged.xlsx");
    const found = await inspect(file, auto);
    expect(found.notes.join(" ")).toMatch(/1 merged range: the value is in each range's first cell/);
    expect(rowsOf(await upload(file, found))).toEqual([
      { region: "North", q: 1n },
      { region: null, q: 2n },
    ]);
  });

  it("reads a sheet with no header row", async () => {
    const book = {
      name: "S",
      rows: [
        [{ n: 1 }, { s: "a" }],
        [{ n: 2 }, { s: "b" }],
      ],
    };
    const file = fileOf(await buildXlsx([book]), "noheader.xlsx");
    const found = await inspect(file, auto);
    expect(found.settings.header).toBe(false);
    expect(found.columns.map((c) => c.name)).toEqual(["column0", "column1"]);
    const forced = await inspect(file, { ...auto, header: "yes" });
    expect(forced.columns.map((c) => c.name)).toEqual(["1", "a"]);
  });

  it("reads .xls and .xlsb workbooks too", async () => {
    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(
      wb,
      XLSX.utils.aoa_to_sheet(
        [
          ["id", "when"],
          [1, new Date(Date.UTC(2026, 4, 6))],
        ],
        { cellDates: true, UTC: true },
      ),
      "Old",
    );
    for (const bookType of ["xls", "xlsb"] as const) {
      const bytes = XLSX.write(wb, { type: "array", bookType }) as ArrayBuffer;
      const file = fileOf(new Uint8Array(bytes), `old.${bookType}`);
      const found = await inspect(file, auto);
      expect(types(found), bookType).toEqual(["BIGINT", "DATE"]);
      expect(rowsOf(await upload(file, found)), bookType).toEqual([{ id: 1n, when: Date.UTC(2026, 4, 6) }]);
    }
  });

  it("says what a date format shows", () => {
    expect(formatShows("m/d/yyyy")).toBe("date");
    expect(formatShows("h:mm:ss AM/PM")).toBe("time");
    expect(formatShows("yyyy-mm-dd hh:mm")).toBe("datetime");
    expect(formatShows("[$-409]mmmm d, yyyy;@")).toBe("date");
    expect(formatShows("mm:ss")).toBe("time");
  });

  it("refuses a file that isn't a workbook, clearly", async () => {
    await expect(inspect(fileOf("id,name\n1,a\n", "renamed.xlsx"), auto)).rejects.toThrow(
      /^renamed\.xlsx isn't an Excel workbook/,
    );
    const file = fileOf(new Uint8Array([0x50, 0x4b, 3, 4, 0, 0]), "broken.xlsx");
    await expect(inspect(file, auto)).rejects.toBeInstanceOf(UploadError);
  });
});

describe("output columns", () => {
  it("reorders, renames and fills NULL for an append to a table with other columns", async () => {
    const file = fileOf("b,a\n2,x\n", "t.csv");
    const found = await inspect(file, auto);
    const plan: OutputColumn[] = [
      { name: "A", type: { kind: "VARCHAR" }, source: 1 },
      { name: "missing", type: { kind: "VARCHAR" }, source: null },
      { name: "B", type: { kind: "DECIMAL", precision: 5, scale: 2 } satisfies ColumnType, source: 0 },
    ];
    const t = await upload(file, found, plan);
    expect(t.schema.fields.map((f) => f.name)).toEqual(["A", "missing", "B"]);
    expect(String(t.schema.fields[2]!.type)).toBe("Decimal[5e+2]");
    const row = rowsOf(t)[0]!;
    expect(row["A"]).toBe("x");
    expect(row["missing"]).toBeNull();
    expect(t.getChildAt(2)!.get(0)!.toString()).toBe("200");
  });
});
