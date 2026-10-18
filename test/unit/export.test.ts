import { tableFromArrays } from "apache-arrow";
import { describe, expect, it } from "vitest";

import { delimitedField, toDelimited, toJson } from "../../src/lib/export";

const table = tableFromArrays({
  id: Int32Array.from([1, 2, 3]),
  label: ["plain", "tab\there", null],
  note: ["a, b", 'say "hi"', " padded "],
});

describe("delimited text", () => {
  it("quotes only what needs quoting", () => {
    expect(delimitedField("plain", ",")).toBe("plain");
    expect(delimitedField("a,b", ",")).toBe('"a,b"');
    expect(delimitedField("a\tb", ",")).toBe("a\tb");
    expect(delimitedField("a\tb", "\t")).toBe('"a\tb"');
    expect(delimitedField('q"q', ",")).toBe('"q""q"');
    expect(delimitedField("line\nbreak", ",")).toBe('"line\nbreak"');
    expect(delimitedField(" x", ",")).toBe('" x"');
  });

  it("writes CSV with a header; NULL is an empty field", () => {
    expect(toDelimited(table, ",")).toBe('id,label,note\n1,plain,"a, b"\n2,tab\there,"say ""hi"""\n3,," padded "\n');
  });

  it("writes a range of rows and columns without a header", () => {
    expect(toDelimited(table, "\t", { header: false, rows: [1, 2], columns: [0, 1] })).toBe('2\t"tab\there"\n3\t\n');
  });

  it("writes nothing for an empty result without a header", () => {
    const empty = tableFromArrays({ id: Int32Array.from([]) });
    expect(toDelimited(empty, ",", { header: false })).toBe("");
    expect(toDelimited(empty, ",")).toBe("id\n");
  });
});

describe("JSON", () => {
  it("writes an array of objects that parses back", () => {
    expect(JSON.parse(toJson(table))).toEqual([
      { id: 1, label: "plain", note: "a, b" },
      { id: 2, label: "tab\there", note: 'say "hi"' },
      { id: 3, label: null, note: " padded " },
    ]);
  });

  it("writes [] for no rows", () => {
    expect(toJson(tableFromArrays({ id: Int32Array.from([]) }))).toBe("[]\n");
  });
});
