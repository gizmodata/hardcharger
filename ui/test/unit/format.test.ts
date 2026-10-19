import { TimeUnit, tableFromArrays, tableFromIPC } from "apache-arrow";
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import {
  formatBinary,
  formatDate,
  formatInterval,
  formatTime,
  idleTimeoutText,
  jsonFor,
  scaleDecimal,
  textFor,
  typeLabel,
} from "../../src/lib/format";

// Arrow IPC of a Hardcharger result with one row of many types (test/unit/fixtures/types.arrows,
// written by `node test/unit/fixtures/make-types.mjs` against a real server).
const types = tableFromIPC(readFileSync(path.join(import.meta.dirname, "fixtures", "types.arrows")));
const row = Object.fromEntries(
  types.schema.fields.map((f, i) => [f.name, textFor(f.type)(types.getChildAt(i)!.get(0))]),
);
const json = Object.fromEntries(
  types.schema.fields.map((f, i) => [f.name, jsonFor(f.type)(types.getChildAt(i)!.get(0))]),
);

describe("text of real server values", () => {
  it("reads numbers exactly", () => {
    expect(row["tiny"]).toBe("1");
    expect(row["big"]).toBe("9007199254740993");
    expect(row["dbl"]).toBe("3.5");
    expect(row["dec"]).toBe("-12.345");
    expect(row["dec_small"]).toBe("0.05");
    expect(row["huge"]).toBe("123456789012345678901234567890");
  });

  it("reads dates, times, timestamps and intervals as DuckDB prints them", () => {
    expect(row["d"]).toBe("2026-10-18");
    expect(row["ts"]).toBe("2026-10-18 01:02:03.456789");
    expect(row["ts_whole"]).toBe("2026-10-18 01:02:03");
    expect(row["tz"]).toBe("2026-10-18 01:02:03+00");
    expect(row["t"]).toBe("01:02:03.5");
    expect(row["iv"]).toBe("1 year 2 months 3 days 04:05:06");
  });

  it("reads nested values, binary, enums and NULL", () => {
    expect(row["l"]).toBe("[1, 2, NULL]");
    expect(row["st"]).toBe("{'k': 1, 'v': z}");
    expect(row["m"]).toBe("{a=1}");
    expect(row["bl"]).toBe("A\\x00\\xFF");
    expect(row["e"]).toBe("b");
    expect(row["n"]).toBeNull();
    expect(row["bo"]).toBe("true");
  });

  it("writes exact JSON", () => {
    expect(json["big"]).toBe("9007199254740993");
    expect(json["dec"]).toBe("-12.345");
    expect(json["huge"]).toBe("123456789012345678901234567890");
    expect(json["d"]).toBe('"2026-10-18"');
    expect(json["l"]).toBe("[1,2,null]");
    expect(json["st"]).toBe('{"k":1,"v":"z"}');
    expect(json["m"]).toBe('{"a":1}');
    expect(json["n"]).toBe("null");
    expect(json["bo"]).toBe("true");
    expect(JSON.parse(`[${Object.values(json).join(",")}]`)).toHaveLength(types.numCols);
  });

  it("labels columns with the server's type names", () => {
    expect(typeLabel(types.schema.fields.find((f) => f.name === "dec")!)).toMatch(/DECIMAL/);
  });
});

describe("formatters", () => {
  it("scales decimals", () => {
    expect(scaleDecimal("12345", 3)).toBe("12.345");
    expect(scaleDecimal("-5", 3)).toBe("-0.005");
    expect(scaleDecimal("7", 0)).toBe("7");
    expect(scaleDecimal("7", -2)).toBe("700");
  });

  it("formats dates before year 1 like DuckDB", () => {
    expect(formatDate(Date.UTC(2026, 9, 18))).toBe("2026-10-18");
    // Epoch milliseconds the server sent for DATE '0001-01-01 (BC)' and '0002-03-04 (BC)'.
    expect(formatDate(-62167219200000)).toBe("0001-01-01 (BC)");
    expect(formatDate(-62193398400000)).toBe("0002-03-04 (BC)");
  });

  it("formats times and intervals", () => {
    expect(formatTime(3723500000n, TimeUnit.MICROSECOND)).toBe("01:02:03.5");
    expect(formatInterval(0, 0, 0n)).toBe("00:00:00");
    expect(formatInterval(-1, -2, -1_500_000_000n)).toBe("-1 month -2 days -00:00:01.5");
  });

  it("escapes binary", () => {
    expect(formatBinary(new Uint8Array([65, 0, 92, 255]))).toBe("A\\x00\\x5C\\xFF");
  });

  it("handles NULL in every column type", () => {
    const t = tableFromArrays({ a: Int32Array.from([1]), s: ["x"] });
    for (const f of t.schema.fields) expect(textFor(f.type)(null)).toBeNull();
  });
});

describe("idleTimeoutText", () => {
  it("names the largest whole unit", () => {
    expect(idleTimeoutText(900)).toBe("15 minutes");
    expect(idleTimeoutText(60)).toBe("1 minute");
    expect(idleTimeoutText(7200)).toBe("2 hours");
    expect(idleTimeoutText(90)).toBe("90 seconds");
    expect(idleTimeoutText(1)).toBe("1 second");
  });
});
