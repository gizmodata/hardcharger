import { Bool, Decimal, Field, Float64, Int64, Struct, Utf8 } from "apache-arrow";
import { describe, expect, it } from "vitest";

import { comparatorFor, sortRows } from "../../src/lib/compare";
import { moveFocus, selectionRect, toClipboardHtml, toClipboardText } from "../../src/lib/gridSelection";
import { looksLikeJson, prettyJson } from "../../src/lib/prettyJson";

describe("moveFocus", () => {
  const at = { row: 5, col: 2 };
  const move = (key: string, ctrlOrMeta = false) =>
    moveFocus({ key, ctrlOrMeta, focus: at, rowCount: 100, colCount: 6, pageRows: 20 });
  it("moves a cell, a screenful, or to an edge", () => {
    expect(move("ArrowDown")).toEqual({ row: 6, col: 2 });
    expect(move("ArrowLeft")).toEqual({ row: 5, col: 1 });
    expect(move("ArrowUp", true)).toEqual({ row: 0, col: 2 });
    expect(move("ArrowRight", true)).toEqual({ row: 5, col: 5 });
    expect(move("Home")).toEqual({ row: 5, col: 0 });
    expect(move("End")).toEqual({ row: 5, col: 5 });
    expect(move("Home", true)).toEqual({ row: 0, col: 0 });
    expect(move("End", true)).toEqual({ row: 99, col: 5 });
    expect(move("PageUp")).toEqual({ row: 0, col: 2 });
    expect(move("PageDown")).toEqual({ row: 25, col: 2 });
    expect(move("a")).toBeNull();
  });
  it("stays inside the grid", () => {
    expect(
      moveFocus({
        key: "ArrowUp",
        ctrlOrMeta: false,
        focus: { row: 0, col: 0 },
        rowCount: 3,
        colCount: 3,
        pageRows: 5,
      }),
    ).toEqual({ row: 0, col: 0 });
    expect(
      moveFocus({
        key: "PageDown",
        ctrlOrMeta: false,
        focus: { row: 1, col: 0 },
        rowCount: 3,
        colCount: 3,
        pageRows: 5,
      }),
    ).toEqual({ row: 2, col: 0 });
  });
  it("selectionRect is the rectangle between anchor and focus, either way round", () => {
    expect(selectionRect({ anchor: { row: 4, col: 3 }, focus: { row: 1, col: 5 } })).toEqual({
      top: 1,
      bottom: 4,
      left: 3,
      right: 5,
    });
  });
});

describe("clipboard forms", () => {
  it("one cell is its bare value; NULL is an empty cell", () => {
    expect(toClipboardText([['say "hi"\tthere']])).toBe('say "hi"\tthere');
    expect(toClipboardText([[null]])).toBe("");
  });
  it("a range quotes what would split a cell, the way spreadsheets read it back", () => {
    expect(
      toClipboardText([
        ["a", "b\tc"],
        ['d"e', null],
        ["line\nbreak", "00123"],
      ]),
    ).toBe('a\t"b\tc"\n"d""e"\t\n"line\nbreak"\t00123');
  });
  it("HTML: a cell per value, line breaks inside the cell, text columns marked as text", () => {
    const html = toClipboardHtml(
      [
        ["<b>", "00123"],
        [null, "x\ny"],
      ],
      [false, true],
    );
    expect(html).toBe(
      '<meta charset="utf-8"><table><tbody><tr><td>&lt;b&gt;</td><td style="mso-number-format:\'\\@\'">00123</td></tr>' +
        '<tr><td></td><td style="mso-number-format:\'\\@\'">x<br style="mso-data-placement:same-cell">y</td></tr></tbody></table>',
    );
  });
});

describe("sorting", () => {
  const text = (v: unknown) => (typeof v === "string" ? v : null);
  const sorted = (type: Parameters<typeof comparatorFor>[0], values: unknown[], dir: "asc" | "desc" = "asc") =>
    sortRows(
      values.map((_, i) => i),
      (i) => values[i],
      comparatorFor(type, text),
      dir,
    ).map((i) => values[i]);

  it("numbers, NULLs last in both directions, NaN after the numbers", () => {
    expect(sorted(new Float64(), [3, null, -1, Number.NaN, 2.5])).toEqual([-1, 2.5, 3, Number.NaN, null]);
    expect(sorted(new Float64(), [3, null, -1, 2.5], "desc")).toEqual([3, 2.5, -1, null]);
    expect(sorted(new Int64(), [10n, null, -(2n ** 63n), 2n ** 62n])).toEqual([-(2n ** 63n), 10n, 2n ** 62n, null]);
  });
  it("decimals by their exact value, beyond a double", () => {
    const big = (s: string) => ({ toString: () => s });
    const values = [big("123456789012345678901234567891"), big("-5"), big("123456789012345678901234567890")];
    expect(sorted(new Decimal(0, 38, 128), values).map(String)).toEqual([
      "-5",
      "123456789012345678901234567890",
      "123456789012345678901234567891",
    ]);
  });
  it("text in byte order, booleans false first; ties keep their order", () => {
    expect(sorted(new Utf8(), ["pear", "Banana", "apple", null, "apple"])).toEqual([
      "Banana",
      "apple",
      "apple",
      "pear",
      null,
    ]);
    expect(sorted(new Bool(), [true, false, null, true])).toEqual([false, true, true, null]);
  });
  it("nested values by their grid text", () => {
    const type = new Struct([new Field("a", new Utf8())]);
    expect(sorted(type, ["b", "a"])).toEqual(["a", "b"]);
  });
});

describe("prettyJson", () => {
  it("re-indents without parsing: big numbers keep every digit", () => {
    expect(prettyJson('{"n":12345678901234567890123,"a":[1,{}],"s":"x, y: {z}"}')).toBe(
      '{\n  "n": 12345678901234567890123,\n  "a": [\n    1,\n    {}\n  ],\n  "s": "x, y: {z}"\n}',
    );
    expect(prettyJson('["a\\"b", []]')).toBe('[\n  "a\\"b",\n  []\n]');
  });
  it("only objects and arrays count as JSON to pretty-print", () => {
    expect(looksLikeJson(' {"a": 1} ')).toBe(true);
    expect(looksLikeJson("[1, 2")).toBe(false);
    expect(looksLikeJson('"text"')).toBe(false);
    expect(looksLikeJson("{not json}")).toBe(false);
  });
});
